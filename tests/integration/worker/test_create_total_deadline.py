"""A create whose answer trickles in past the send lease, with two workers on PostgreSQL.

Each piece of the answer arrives well within the per-read timeout, so only the client's total
deadline ends the call. It must end, as a transient error, before the first worker's lease
can pass to the second worker, so the provider never has two POSTs of one reference open at
once and the second worker looks the reference up before its own POST. Real sockets on
127.0.0.1 (``LocalSePay``); the clock is the system clock."""

import asyncio
import json

import httpx
import pytest
from tests.contract.sepay_einvoice.conftest import RECORDED, Answer, LocalSePay, read_json
from tests.integration.worker.world import World

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.application.issue_queued import IssueResult
from invoice_module.builder import InvoiceModule, build_invoice_module
from invoice_module.ports.clock import SystemClock

pytestmark = pytest.mark.integration

CREATE = "/v1/invoices/create"
# The default ratio, scaled down: the lease is three times the HTTP timeout.
CONFIG = InvoiceModuleConfig(lease_seconds=3, http_timeout_s=1.0)
# Eight pieces 0.6 s apart: every read waits 0.6 s, the whole answer 4.8 s, past the lease.
CHUNKS, GAP_S = 8, 0.6
TOKEN = {"success": True, "data": {"access_token": "synthetic-token", "expires_in": 86400}}
NOT_FOUND = {"success": False, "error": {"code": "EINVOICE_DOCUMENT_NOT_FOUND"}}
# The recorded status check answer for a tracking code the provider never received.
NO_JOB = read_json(RECORDED / "check_unknown_tracking.json")
CHECK = "/v1/invoices/create/check/"


def sandbox(method: str, path: str, body: bytes) -> Answer:
    if path == "/v1/token":
        return Answer(200, TOKEN)
    if (method, path) == ("POST", CREATE):
        accepted = {"tracking_code": json.loads(body)["reference_code"]}
        return Answer(202, {"success": True, "data": accepted}, chunks=CHUNKS, gap_s=GAP_S)
    assert method == "GET" and path.startswith("/v1/invoices/"), path
    if path.startswith(CHECK):
        return Answer(NO_JOB["status"], NO_JOB["body"])
    return Answer(404, NOT_FOUND)


def worker(world: World, provider: SePayEInvoiceProvider) -> InvoiceModule:
    return build_invoice_module(
        world.crash,
        SystemClock(),
        world.metrics,
        provider=provider,
        credentials=world.credentials,
        artifact_store=world.store,
        config=CONFIG,
    )


async def test_a_trickled_create_ends_before_its_lease_and_never_overlaps_another_post(world):
    world.clock.set(SystemClock().now())
    settled = await world.settle()
    server = LocalSePay(sandbox)
    async with server, httpx.AsyncClient(transport=server.transport()) as http:
        client = SePayEInvoiceClient(
            http,
            world.credentials,
            SystemClock(),
            ProcessRateLimiter(1000.0),
            http_timeout_s=CONFIG.http_timeout_s,
        )
        provider = SePayEInvoiceProvider(client)
        first, second = worker(world, provider), worker(world, provider)
        assert first.issue_queued and second.issue_queued
        loop = asyncio.get_running_loop()
        started = loop.time()
        ended: list[float] = []
        sending = asyncio.create_task(first.issue_queued.run_batch())
        sending.add_done_callback(lambda _: ended.append(loop.time()))
        # The second worker keeps trying until it has handled the document and the whole
        # trickled answer would have been sent, the window in which a second POST could
        # overlap the first.
        later: list[IssueResult] = []
        while not later or loop.time() - started < CHUNKS * GAP_S:
            assert loop.time() - started < 30, later
            await asyncio.sleep(0.1)
            later += await second.issue_queued.run_batch()
        results = await sending

    posts = server.seen("POST", CREATE)
    assert LocalSePay.most_open_at_once(posts) == 1
    # The first call ended as transient before its lease, taken after ``started``, ran out.
    assert results == [IssueResult(settled.document_id, "backoff")]
    assert ended[0] - started < CONFIG.lease_seconds
    assert [json.loads(post.body)["reference_code"] for post in posts] == [settled.reference] * 2
    assert posts[0].completed is False
    assert posts[0].closed is not None and posts[0].closed - started < CONFIG.lease_seconds
    # The second worker looked the reference up and checked its job after the first POST ended,
    # then sent its own.
    [lookup] = server.seen("GET", f"/v1/invoices/{settled.reference}")
    [check] = server.seen("GET", f"{CHECK}{settled.reference}")
    assert posts[0].closed <= lookup.opened < check.opened < posts[1].opened
    assert later == [IssueResult(settled.document_id, "backoff")]
    document = await world.doc(settled.document_id)
    assert (document.status, document.reference_code) == ("queued", settled.reference)
    assert document.post_count == 2
