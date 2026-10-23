from datetime import timedelta

import pytest
from tests.fakes.fake_einvoice_provider import TRANSIENT
from tests.integration.worker.world import World

from invoice_module.application.hold_draft_invoice import HoldNotAllowed
from invoice_module.builder import build_invoice_module
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.errors import DocumentInFlight

pytestmark = pytest.mark.integration

MANUAL = DeliveryMode.MANUAL


async def awaiting(world):
    settled = await world.settle(mode=MANUAL)
    await world.issue()
    return settled


async def hold(world, settled, actor="accountant-a", note="Wrong buyer name"):
    return await world.module.hold_draft_invoice.execute(settled.document_id, actor, note)


async def test_hold_awaiting_moves_to_blocked_with_note_and_actor(world):
    settled = await awaiting(world)
    before = await world.doc(settled.document_id)
    result = await hold(world, settled)
    doc = await world.doc(settled.document_id)
    assert (result.status, doc.status, doc.next_attempt_at) == ("blocked", "blocked", None)
    assert doc.lease_generation == before.lease_generation + 1
    assert await world.reason_details(settled.document_id, "DRAFT_HELD") == {
        "note": "Wrong buyer name",
        "held_by": "accountant-a",
    }
    assert world.metrics.count("invoice_draft_held_total") == 1
    await world.drive(rounds=3)
    assert world.provider.calls == []


@pytest.mark.parametrize(
    "case", ["unsent", "send_started", "pending_provider", "lost_create_retry"]
)
async def test_hold_approved_only_before_send_started(world, case):
    settled = await awaiting(world)
    await world.approve(settled)
    if case == "send_started":
        world.provider.script_create(settled.reference, TRANSIENT)
        await world.issue()
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.send_started_at is not None) == ("approved", True)
        world.past_lease()
    elif case == "pending_provider":
        await world.issue()
    elif case == "lost_create_retry":
        world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT)
        await world.issue()
        world.past_lease()
        await world.issue()
        world.past_lease()
        await world.issue()
        assert (await world.doc(settled.document_id)).status == "failed"
        await world.resolve(settled.document_id, "retry")
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.attempts, doc.send_started_at is not None) == ("approved", 0, True)
    if case == "unsent":
        await hold(world, settled)
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.approved_at) == ("blocked", None)
        case_row = await world.open_case(settled.document_id)
        assert case_row.details["previous_approvals"][0]["reason"] == "held"
        return
    before = await world.doc(settled.document_id)
    for _ in range(2):
        with pytest.raises(HoldNotAllowed) as caught:
            await hold(world, settled)
        assert caught.value.reason == "send_started"
        world.restart()
    assert await world.doc(settled.document_id) == before


async def test_hold_requires_actor_and_note(world):
    settled = await awaiting(world)
    before = await world.doc(settled.document_id)
    for actor, note in (("", "note"), (" ", "note"), ("accountant-a", ""), ("accountant-a", "\t")):
        with pytest.raises(ValueError):
            await hold(world, settled, actor, note)
    assert await world.doc(settled.document_id) == before
    assert await world.cases(settled.document_id) == []


async def test_hold_fences_worker_claim(world):
    settled = await awaiting(world)
    await world.approve(settled)
    outcomes = []

    async def operator_holds_during_claim():
        try:
            await hold(world, settled)
        except DocumentInFlight:
            outcomes.append("in_flight")
        # The claim stalls past its lease; the hold then wins and fences the stale worker.
        world.past_lease()
        await hold(world, settled)
        outcomes.append("held")

    world.provider.on_next_prepare(operator_holds_during_claim)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert outcomes == ["in_flight", "held"]
    assert result.outcome == "lease_lost" and world.provider.posts() == []
    assert (doc.status, doc.post_count, doc.send_started_at) == ("blocked", 0, None)


async def test_hold_needs_no_provider(engine, inv_tables, tmp_path):
    world = World(engine, inv_tables, tmp_path)
    await world.seed_account()
    settled = await awaiting(world)
    offline = build_invoice_module(world.crash, world.clock, world.metrics)
    assert (offline.issue_queued, offline.poll_pending, offline.resolve_review) == (
        None,
        None,
        None,
    )
    assert offline.escalate_overdue is None and offline.approve_draft_invoice is not None
    await offline.hold_draft_invoice.execute(settled.document_id, "accountant-a", "Wrong")
    assert (await world.doc(settled.document_id)).status == "blocked"
    assert world.provider.calls == []


async def test_corrected_content_after_hold_requires_fresh_approval(world):
    settled = await awaiting(world)
    await world.approve(settled)
    await hold(world, settled)
    await world.resolve(
        settled.document_id,
        "attach_draft",
        draft=world.draft_command(
            settled.host_ref_id, settled.intent_id, mode=MANUAL, name="Corrected item"
        ),
    )
    assert (await world.doc(settled.document_id)).status == "queued"
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at, doc.reference_code) == (
        "awaiting_approval",
        None,
        settled.reference,
    )
    assert doc.request["items"][0]["item_name"] == "Corrected item"
    [case] = await world.cases(settled.document_id)
    assert [entry["approved_by"] for entry in case.details["previous_approvals"]] == [
        "accountant-a"
    ]
    world.advance(timedelta(minutes=1))
    await world.approve(settled, actor="accountant-b")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_by) == ("approved", "accountant-b")
    assert world.provider.calls == []
