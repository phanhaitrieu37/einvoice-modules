from datetime import timedelta

import pytest
from tests.integration.worker.world import vn

from invoice_module.application.mark_cancelled_externally import CancellationNotAllowed

pytestmark = pytest.mark.integration


async def issued(world):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    return settled


async def test_marks_issued_as_cancelled_with_audit(world):
    settled = await issued(world)
    before = await world.doc(settled.document_id)
    world.clock.set(vn(2026, 10, 22, 14))
    for actor, reason in (("", "x"), ("operator-a", "  ")):
        with pytest.raises(ValueError):
            await world.module.mark_cancelled_externally.execute(settled.document_id, reason, actor)
    result = await world.module.mark_cancelled_externally.execute(
        settled.document_id, "Cancelled at the portal on the buyer's request", "operator-a"
    )
    doc = await world.doc(settled.document_id)
    assert result.status == "cancelled_externally"
    assert (doc.status, doc.cancelled_at, doc.cancelled_by, doc.cancel_reason) == (
        "cancelled_externally",
        vn(2026, 10, 22, 14),
        "operator-a",
        "Cancelled at the portal on the buyer's request",
    )
    assert (doc.invoice_number, doc.lease_generation) == (
        before.invoice_number,
        before.lease_generation + 1,
    )


@pytest.mark.parametrize("state", ["queued", "failed", "pending_provider", "cancelled"])
async def test_refuses_non_issued(world, state):
    settled = await world.settle()
    if state != "queued":
        await world.issue()
    if state == "failed":
        await world.set_columns(settled.document_id, status="failed")
    if state == "cancelled":
        world.advance(timedelta(seconds=3))
        await world.poll()
        await world.module.mark_cancelled_externally.execute(settled.document_id, "r", "op")
    before = await world.doc(settled.document_id)
    with pytest.raises(CancellationNotAllowed):
        await world.module.mark_cancelled_externally.execute(settled.document_id, "r", "op")
    assert await world.doc(settled.document_id) == before


async def test_never_calls_provider(world):
    settled = await issued(world)
    calls = list(world.provider.calls)
    await world.module.mark_cancelled_externally.execute(settled.document_id, "Portal", "op")
    await world.drive(rounds=2)
    await world.artifacts()
    assert [c for c in world.provider.calls[len(calls) :] if c.op != "fetch_artifacts"] == []
