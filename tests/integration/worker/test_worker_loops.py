import asyncio
from datetime import timedelta

import pytest

from invoice_module.runtime import InvoiceWorker, WorkerIntervals

pytestmark = pytest.mark.integration

FAST = WorkerIntervals(issue_s=0.01, poll_s=0.01, sweep_s=0.01, artifacts_s=0.01)


async def run_until(world, worker, done, timeout=20.0):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        async with asyncio.timeout(timeout):
            while not await done():
                await asyncio.sleep(0.02)
                world.advance(timedelta(seconds=5))
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    return task


async def test_worker_issues_polls_and_fetches_until_stop(world):
    settled = await world.settle()
    worker = InvoiceWorker(world.module, FAST)
    assert set(worker.loops()) == {"issue", "poll", "sweep", "artifacts"}

    async def stored():
        doc = await world.doc(settled.document_id)
        return doc.status == "issued" and doc.pdf_uri is not None and doc.xml_uri is not None

    task = await run_until(world, worker, stored)
    assert task.done() and task.exception() is None
    assert set(world.provider.ops()) == {"create", "check_create", "fetch_artifacts"}
    assert len(world.provider.posts()) == 1


async def test_failing_step_is_counted_and_loop_continues(world, monkeypatch):
    settled = await world.settle()
    issue_queued = world.module.issue_queued
    original = issue_queued.run_batch
    failures = []

    async def flaky(limit=None):
        if not failures:
            failures.append(1)
            raise RuntimeError("database unavailable")
        return await original(limit)

    monkeypatch.setattr(issue_queued, "run_batch", flaky)

    async def sent():
        return (await world.doc(settled.document_id)).status in ("pending_provider", "issued")

    await run_until(world, InvoiceWorker(world.module, FAST), sent)
    assert failures == [1]
    assert world.metrics.count("worker_loop_errors_total", loop="issue") == 1
    assert len(world.provider.posts()) == 1


async def test_sweep_failure_does_not_block_polling(world, monkeypatch):
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, pending_polls=10_000)
    await world.issue()
    assert (await world.doc(settled.document_id)).status == "pending_provider"
    sweeps = []

    async def broken(now=None):
        sweeps.append(now)
        raise RuntimeError("sweep query failed")

    monkeypatch.setattr(world.module.escalate_overdue, "run", broken)
    worker = InvoiceWorker(world.module, FAST)

    async def polled_while_sweep_fails():
        checks = [call for call in world.provider.calls if call.op == "check_create"]
        return len(sweeps) >= 2 and len(checks) >= 2

    await run_until(world, worker, polled_while_sweep_fails)
    assert world.metrics.count("worker_loop_errors_total", loop="sweep") >= 2
    assert world.metrics.count("worker_loop_errors_total", loop="poll") == 0
