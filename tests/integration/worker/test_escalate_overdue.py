from datetime import date, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from tests.fakes.fake_einvoice_provider import CONFLICT, FakeInvoice
from tests.integration.worker.world import TENANT, vn

from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.domain.enums import DeliveryMode

pytestmark = pytest.mark.integration

FRIDAY_10 = vn(2026, 10, 23, 10)
MONDAY_10 = vn(2026, 10, 26, 10)


def items(amount):
    return [
        {
            "name": "Synthetic service",
            "unit": "each",
            "quantity": "1",
            "unit_price_vnd": amount,
            "tax_rate": 0,
            "discount_vnd": 0,
            "tax_amount_vnd": None,
            "item_code": None,
        }
    ]


async def insert_draft(world, settled, *, intent_id, amount=100):
    draft_id = uuid4()
    async with world.engine.begin() as conn:
        await conn.execute(
            world.tables.invoice_drafts.insert().values(
                id=draft_id,
                tenant_id=TENANT,
                merchant_id=world.merchant_id,
                host_ref_type="order",
                host_ref_id=settled.host_ref_id,
                intent_id=intent_id,
                buyer=None,
                items=items(amount),
                delivery_mode="auto",
                declared_total_vnd=amount,
                computed_total_vnd=amount,
                revision=1,
                status="open",
            )
        )
    return draft_id


async def draft_row(world, draft_id):
    table = world.tables.invoice_drafts
    async with world.engine.connect() as conn:
        return (await conn.execute(sa.select(table).where(table.c.id == draft_id))).one()


async def escalate(world, now=None):
    return await world.module.escalate_overdue.run(now or world.clock.now())


async def rotated(world, mode=DeliveryMode.AUTO):
    """A document whose first reference the operator retired after reconciling at the portal."""
    if mode == DeliveryMode.MANUAL:
        settled = await world.settle(mode=mode)
        await world.issue()
        await world.approve(settled)
    else:
        settled = await world.settle()
    world.provider.script_create(settled.reference, CONFLICT)
    await world.issue()
    assert (await world.doc(settled.document_id)).status == "failed"
    await world.resolve(
        settled.document_id,
        "retry_with_new_reference",
        note="Reconciled at the portal: no invoice for the old reference",
    )
    return settled


async def duplicate_with_mismatch(world):
    first = await world.settle()
    second_intent = uuid4()
    await world.module.submit_draft.execute(world.draft_command(first.host_ref_id, second_intent))
    second = await world.settle_again(first, amount=150, intent_id=second_intent)
    assert sorted(await world.open_reasons(second.document_id)) == [
        "AMOUNT_MISMATCH",
        "DUPLICATE_SETTLEMENT",
    ]
    return first, second


async def fix_draft_amount(world, settled, amount):
    table = world.tables.invoice_drafts
    async with world.engine.begin() as conn:
        await conn.execute(
            table.update()
            .where(table.c.host_ref_id == settled.host_ref_id)
            .values(items=items(amount), declared_total_vnd=amount, computed_total_vnd=amount)
        )


async def test_sweep_attaches_draft_missed_by_wakeup(world):
    settled = await world.settle(draft=False)
    draft_id = await insert_draft(world, settled, intent_id=settled.intent_id)
    report = await escalate(world)
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.draft_id, doc.total_vnd) == ("queued", draft_id, 100)
    assert (await draft_row(world, draft_id)).status == "consumed"
    assert report.drafts_attached == [settled.document_id]
    assert world.metrics.count("invoice_draft_attached_by_sweep_total") == 1
    await escalate(world)
    assert world.metrics.count("invoice_draft_attached_by_sweep_total") == 1


async def test_sweep_respects_intent_binding(world):
    settled = await world.settle(draft=False)
    draft_id = await insert_draft(world, settled, intent_id=uuid4())
    await escalate(world)
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.draft_id, doc.document) == ("needs_draft", None, None)
    assert (await draft_row(world, draft_id)).status == "open"


async def test_draft_missing_review_after_one_hour_and_autorecovers_on_late_draft(world):
    settled = await world.settle(draft=False)
    world.advance(timedelta(minutes=61))
    report = await escalate(world)
    assert report.draft_missing_opened == [settled.document_id]
    assert await world.open_reasons(settled.document_id) == ["DRAFT_MISSING"]
    await escalate(world)
    assert len(await world.cases(settled.document_id)) == 1
    await world.module.submit_draft.execute(
        world.draft_command(settled.host_ref_id, settled.intent_id)
    )
    doc = await world.doc(settled.document_id)
    [case] = await world.cases(settled.document_id)
    assert doc.status == "queued" and case.status == "resolved"
    assert await world.reasons(settled.document_id) == [
        ("DRAFT_MISSING", "cleared", "attach_draft")
    ]


async def test_no_draft_missing_review_before_one_hour(world):
    settled = await world.settle(draft=False)
    world.advance(timedelta(minutes=59))
    report = await escalate(world)
    assert report.draft_missing_opened == [] and await world.cases(settled.document_id) == []


async def test_pending_over_three_hours_fails_with_timeout_review(world):
    world.config = InvoiceModuleConfig(pending_timeout=timedelta(minutes=30))
    world.restart()
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, pending_polls=10_000)
    await world.issue()
    world.advance(timedelta(minutes=29))
    assert (await escalate(world)).timed_out == []
    world.advance(timedelta(minutes=2))
    report = await escalate(world)
    doc = await world.doc(settled.document_id)
    assert report.timed_out == [settled.document_id]
    assert (doc.status, doc.last_error_code) == ("failed", "provider_timeout")
    assert await world.open_reasons(settled.document_id) == ["PROVIDER_TIMEOUT"]
    # The default timeout is three hours.
    assert InvoiceModuleConfig().pending_timeout == timedelta(hours=3)


async def test_stale_alert_after_fifteen_minutes_despite_frequent_polls(world):
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, pending_polls=10_000)
    await world.issue()
    for minute in range(16):
        world.advance(timedelta(minutes=1))
        await world.set_columns(settled.document_id, next_attempt_at=world.clock.now())
        await world.poll()
        report = await escalate(world)
        stale = (settled.document_id, "pending_provider") in report.stale
        assert stale == (minute == 15), minute
    assert world.metrics.count("invoice_documents_stale_total", status="pending_provider") == 1
    assert (await world.doc(settled.document_id)).poll_count == 16


async def test_artifacts_missing_alert_after_one_hour(world):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    world.advance(timedelta(minutes=59))
    assert (await escalate(world)).artifacts_missing == []
    world.advance(timedelta(minutes=2))
    assert (await escalate(world)).artifacts_missing == [settled.document_id]
    assert world.metrics.count("invoice_artifacts_missing_total") == 1
    await world.artifacts()
    assert (await escalate(world)).artifacts_missing == []


async def test_review_overdue_counts_business_days(world):
    settled = await world.settle(draft=False)
    world.advance(timedelta(hours=2))
    await escalate(world)
    case = await world.open_case(settled.document_id)
    async with world.engine.begin() as conn:
        table = world.tables.review_cases
        await conn.execute(table.update().where(table.c.id == case.id).values(opened_at=FRIDAY_10))
    assert (await escalate(world, vn(2026, 10, 25, 23))).reviews_overdue == []
    assert (await escalate(world, MONDAY_10 - timedelta(minutes=1))).reviews_overdue == []
    assert (await escalate(world, MONDAY_10)).reviews_overdue == [case.id]
    assert world.metrics.count("invoice_review_overdue_total") == 1


async def test_previous_reference_recheck_opens_review_when_found(world):
    settled = await rotated(world)
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 100))
    world.advance(timedelta(hours=1))
    report = await escalate(world)
    assert report.stale_references == [(settled.document_id, settled.reference)]
    details = await world.reason_details(settled.document_id, "STALE_REFERENCE_FOUND")
    assert details == {
        "previous_reference": settled.reference,
        "provider_state": "success",
        "provider_invoice_id": f"inv-{settled.reference}",
    }
    assert world.metrics.count("invoice_stale_reference_found_total") == 1
    await world.resolve(
        settled.document_id, "acknowledge", note="Old invoice handled at the portal"
    )
    assert (await escalate(world)).stale_references == []
    assert world.metrics.count("invoice_stale_reference_found_total") == 1


async def test_previous_reference_recheck_stops_after_window(world):
    settled = await rotated(world)
    await world.issue()
    world.advance(timedelta(hours=24, seconds=1))
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 100))
    calls = len(world.provider.calls)
    report = await escalate(world)
    assert report.stale_references == []
    assert all(c.reference_code != settled.reference for c in world.provider.calls[calls:])


async def test_previous_reference_recheck_skips_awaiting_approval(world):
    settled = await rotated(world, mode=DeliveryMode.MANUAL)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.status == "awaiting_approval" and doc.reference_code != settled.reference
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 100))
    calls = len(world.provider.calls)
    for _ in range(3):
        await escalate(world)
        await world.drive(rounds=1)
    assert world.provider.calls[calls:] == []
    await world.module.hold_draft_invoice.execute(
        settled.document_id, "accountant-a", "Wrong buyer name"
    )
    for _ in range(3):
        await escalate(world)
        await world.drive(rounds=1)
    assert world.provider.calls[calls:] == []
    assert (await world.doc(settled.document_id)).status == "blocked"


async def test_approval_overdue_alert_after_threshold(world):
    world.clock.set(FRIDAY_10)
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    report = await escalate(world, MONDAY_10)
    assert report.approvals_overdue == [settled.document_id]
    assert world.metrics.count("invoice_approval_overdue_total") == 1
    # The same business-day rule as the review SLO.
    world.clock.set(MONDAY_10)
    overdue = await world.module.list_pending_approvals.execute(overdue_only=True)
    assert [item.document_id for item in overdue] == [settled.document_id]


async def test_no_approval_alert_before_threshold(world):
    world.clock.set(FRIDAY_10)
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    for moment in (
        vn(2026, 10, 24, 10),
        vn(2026, 10, 25, 23, 59),
        MONDAY_10 - timedelta(seconds=1),
    ):
        assert (await escalate(world, moment)).approvals_overdue == []
    assert world.metrics.count("invoice_approval_overdue_total") == 0
    assert (await world.doc(settled.document_id)).status == "awaiting_approval"


async def test_approved_stale_alert_after_fifteen_minutes(world):
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    await world.approve(settled)
    world.advance(timedelta(minutes=14))
    assert (await escalate(world)).stale == []
    world.advance(timedelta(minutes=2))
    assert (await escalate(world)).stale == [(settled.document_id, "approved")]
    assert world.metrics.count("invoice_documents_stale_total", status="approved") == 1


async def test_sweep_keeps_duplicate_hold_blocked(world):
    _, second = await duplicate_with_mismatch(world)
    await fix_draft_amount(world, second, 150)
    report = await escalate(world)
    doc = await world.doc(second.document_id)
    assert report.drafts_attached == [second.document_id]
    assert (doc.status, doc.total_vnd, doc.queued_at) == ("blocked", 150, None)
    assert await world.open_reasons(second.document_id) == ["DUPLICATE_SETTLEMENT"]


async def test_sweep_keeps_waived_duplicate_blocked(world):
    _, second = await duplicate_with_mismatch(world)
    await world.resolve(
        second.document_id,
        "waive",
        note="Refunded to the buyer outside the module",
        reason="DUPLICATE_SETTLEMENT",
    )
    await fix_draft_amount(world, second, 150)
    await escalate(world)
    await world.drive(rounds=3)
    doc = await world.doc(second.document_id)
    assert (doc.status, doc.total_vnd) == ("blocked", 150)
    assert all(c.reference_code != second.reference for c in world.provider.calls)


async def test_overdue_approval_alert_does_not_transition(world, transitions):
    world.clock.set(FRIDAY_10)
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    transitions.edges.clear()
    report = await escalate(world, MONDAY_10)
    assert report.approvals_overdue == [settled.document_id]
    assert all("awaiting_approval" not in edge for edge in transitions.edges)
    assert (await world.doc(settled.document_id)).status == "awaiting_approval"
