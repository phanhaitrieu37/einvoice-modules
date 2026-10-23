import logging
from datetime import date, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from tests.fakes.fake_einvoice_provider import CONFLICT, FakeInvoice
from tests.integration.worker.world import TENANT, vn

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRepository
from invoice_module.application import escalate_overdue as escalate_module
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


async def insert_draft(
    world, settled=None, *, intent_id, amount=100, host_ref_id=None, merchant_id=None, lines=None
):
    draft_id = uuid4()
    async with world.engine.begin() as conn:
        await conn.execute(
            world.tables.invoice_drafts.insert().values(
                id=draft_id,
                tenant_id=TENANT,
                merchant_id=merchant_id or world.merchant_id,
                host_ref_type="order",
                host_ref_id=host_ref_id or settled.host_ref_id,
                intent_id=intent_id,
                buyer=None,
                items=items(amount) if lines is None else lines,
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


def sweep_errors(world, step):
    return world.metrics.count("invoice_sweep_step_errors_total", step=step)


async def settle_pending(world):
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, pending_polls=10_000)
    await world.issue()
    assert (await world.doc(settled.document_id)).status == "pending_provider"
    return settled


async def test_poison_candidate_does_not_stop_the_sweep(world):
    pending = await settle_pending(world)
    poison = await world.settle(draft=False)
    # A draft row the sweep cannot read (its line has no unit) stands for any persistent error
    # on one candidate; it sorts before the attachable document.
    broken = [{key: value for key, value in items(100)[0].items() if key != "unit"}]
    await insert_draft(world, poison, intent_id=poison.intent_id, lines=broken)
    world.advance(timedelta(seconds=1))
    late = await world.settle(draft=False)
    await insert_draft(world, late, intent_id=late.intent_id)
    world.advance(timedelta(hours=3, minutes=1))
    report = await escalate(world)
    assert report.errors == [("attach_draft", "KeyError")]
    assert sweep_errors(world, "attach_draft") == 1
    assert report.drafts_attached == [late.document_id]
    assert (await world.doc(late.document_id)).status == "queued"
    assert (await world.doc(poison.document_id)).status == "needs_draft"
    assert report.timed_out == [pending.document_id]
    assert (await world.doc(pending.document_id)).status == "failed"


async def test_failing_sub_sweep_does_not_stop_the_others(world, monkeypatch):
    pending = await settle_pending(world)

    async def broken(self, limit):
        raise RuntimeError("query failed")

    monkeypatch.setattr(DocumentRepository, "list_awaiting_draft", broken)
    world.advance(timedelta(hours=3, minutes=1))
    report = await escalate(world)
    assert report.errors == [("attach_drafts", "RuntimeError")]
    assert sweep_errors(world, "attach_drafts") == 1
    assert report.timed_out == [pending.document_id]


@pytest.mark.parametrize("stuck_by", ["no_draft", "foreign_draft"])
async def test_draft_sweep_reaches_document_beyond_batch_of_stuck_documents(world, stuck_by):
    world.config = InvoiceModuleConfig(batch_size=2)
    world.restart()
    stuck = []
    for _ in range(2):  # orders with no draft of their merchant to attach
        stuck.append(await world.settle(draft=False))
        if stuck_by == "foreign_draft":
            await foreign_draft(world, stuck[-1].host_ref_id)
        world.advance(timedelta(seconds=1))
    late = await world.settle(draft=False)
    draft_id = await insert_draft(world, late, intent_id=late.intent_id)  # the wake-up was missed
    report = await escalate(world)
    doc = await world.doc(late.document_id)
    assert (doc.status, doc.draft_id) == ("queued", draft_id)
    assert report.drafts_attached == [late.document_id]
    assert [(await world.doc(s.document_id)).status for s in stuck] == ["needs_draft"] * 2


async def test_draft_sweep_reaches_document_behind_mismatched_open_drafts(world):
    world.config = InvoiceModuleConfig(batch_size=2)
    world.restart()
    stuck = []
    for _ in range(2):  # settled at 100 while their open draft still declares 150
        stuck.append(await world.settle(draft_amount=150))
        assert await world.open_reasons(stuck[-1].document_id) == ["AMOUNT_MISMATCH"]
        world.advance(timedelta(seconds=1))
    before = [await world.doc(s.document_id) for s in stuck]
    late = await world.settle(draft=False)
    draft_id = await insert_draft(world, late, intent_id=late.intent_id)  # the wake-up was missed
    report = await escalate(world)
    doc = await world.doc(late.document_id)
    assert (doc.status, doc.draft_id) == ("queued", draft_id)
    assert report.drafts_attached == [late.document_id]
    after = [await world.doc(s.document_id) for s in stuck]
    assert [(d.status, d.updated_at) for d in after] == [(d.status, d.updated_at) for d in before]
    assert {d.status for d in after} == {"blocked"}
    # A corrected revision of the same draft row is attachable again.
    await fix_draft_amount(world, stuck[0], 100)
    report = await escalate(world)
    assert report.drafts_attached == [stuck[0].document_id]
    assert (await world.doc(stuck[0].document_id)).status == "queued"


@pytest.mark.parametrize("older", ["waived", "open"])
async def test_draft_missing_reaches_newest_document_behind_decided_ones(world, monkeypatch, older):
    monkeypatch.setattr(escalate_module, "_ALERT_SCAN_LIMIT", 2)
    decided = []
    for _ in range(2):
        decided.append(await world.settle(draft=False))
        world.advance(timedelta(seconds=1))
    world.advance(timedelta(minutes=61))
    assert (await escalate(world)).draft_missing_opened == [s.document_id for s in decided]
    if older == "waived":
        for settled in decided:
            await world.resolve(settled.document_id, "waive", note="Invoiced by hand at the portal")
    newest = await world.settle(draft=False)
    world.advance(timedelta(minutes=61))
    report = await escalate(world)
    assert report.draft_missing_opened == [newest.document_id]
    assert await world.open_reasons(newest.document_id) == ["DRAFT_MISSING"]


async def test_sweep_does_not_reopen_waived_draft_missing(world):
    settled = await world.settle(draft=False)
    world.advance(timedelta(minutes=61))
    await escalate(world)
    await world.resolve(settled.document_id, "waive", note="Invoiced by hand at the portal")
    for _ in range(3):
        world.advance(timedelta(minutes=5))
        assert (await escalate(world)).draft_missing_opened == []
    [case] = await world.cases(settled.document_id)
    assert case.status == "resolved"
    assert await world.open_reasons(settled.document_id) == []
    assert (await world.doc(settled.document_id)).status == "needs_draft"


async def test_artifacts_missing_alert_reaches_newest_issued_document(world, monkeypatch):
    monkeypatch.setattr(escalate_module, "_ALERT_SCAN_LIMIT", 2)
    documents = []
    for _ in range(3):
        settled = await world.settle()
        await world.issue()
        world.advance(timedelta(seconds=4))
        await world.poll()
        documents.append(settled)
        world.advance(timedelta(minutes=1))
    newest = documents[-1]
    world.provider.fail_artifacts(newest.reference, times=1_000)
    await world.artifacts()
    complete = [await world.doc(s.document_id) for s in documents[:2]]
    assert all(doc.pdf_uri is not None and doc.xml_uri is not None for doc in complete)
    assert (await world.doc(newest.document_id)).pdf_uri is None
    world.advance(timedelta(hours=2))
    assert (await escalate(world)).artifacts_missing == [newest.document_id]


async def test_stale_alert_reaches_newest_queued_document(world, monkeypatch):
    monkeypatch.setattr(escalate_module, "_ALERT_SCAN_LIMIT", 2)
    late_drafts = []
    for _ in range(2):  # settled first, queued only when their draft arrives later
        late_drafts.append(await world.settle(draft=False))
        world.advance(timedelta(seconds=1))
    newest = await world.settle()
    world.advance(timedelta(minutes=20))
    for settled in late_drafts:
        await world.module.submit_draft.execute(
            world.draft_command(settled.host_ref_id, settled.intent_id)
        )
        assert (await world.doc(settled.document_id)).status == "queued"
    world.advance(timedelta(minutes=1))
    assert (await escalate(world)).stale == [(newest.document_id, "queued")]


async def test_previous_reference_recheck_throttled_within_interval(world):
    assert InvoiceModuleConfig().previous_reference_recheck_interval == timedelta(minutes=10)
    settled = await rotated(world)

    def rechecks():
        return [
            call
            for call in world.provider.calls
            if call.op == "find_by_reference" and call.reference_code == settled.reference
        ]

    before = len(rechecks())
    await escalate(world)
    world.advance(timedelta(minutes=1))
    await escalate(world)
    assert len(rechecks()) == before + 1
    world.advance(timedelta(minutes=10))
    await escalate(world)
    assert len(rechecks()) == before + 2


async def test_stale_alert_logs_once_per_entry_but_counts_every_sweep(world, caplog):
    settled = await world.settle()
    world.advance(timedelta(minutes=16))
    with caplog.at_level(logging.ERROR, logger=escalate_module.__name__):
        for _ in range(3):
            assert (await escalate(world)).stale == [(settled.document_id, "queued")]
            world.advance(timedelta(seconds=30))
    logged = [record for record in caplog.records if record.message == "invoice_document_stale"]
    assert len(logged) == 1 and logged[0].document_id == str(settled.document_id)
    assert world.metrics.count("invoice_documents_stale_total", status="queued") == 3


async def test_approval_overdue_logs_once_per_entry_but_counts_every_sweep(world, caplog):
    world.clock.set(FRIDAY_10)
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    with caplog.at_level(logging.ERROR, logger=escalate_module.__name__):
        for minute in range(3):
            report = await escalate(world, MONDAY_10 + timedelta(minutes=minute))
            assert report.approvals_overdue == [settled.document_id]
    logged = [record for record in caplog.records if record.message == "invoice_approval_overdue"]
    assert len(logged) == 1
    assert world.metrics.count("invoice_approval_overdue_total") == 3


async def test_timeout_sweep_skips_pending_document_with_live_poll_lease(world):
    settled = await settle_pending(world)
    world.advance(timedelta(hours=3, minutes=1))
    await world.set_columns(
        settled.document_id,
        lease_owner="poller",
        lease_until=world.clock.now() + timedelta(seconds=30),
    )
    assert (await escalate(world)).timed_out == []
    assert (await world.doc(settled.document_id)).status == "pending_provider"
    world.advance(timedelta(seconds=31))
    assert (await escalate(world)).timed_out == [settled.document_id]
    assert (await world.doc(settled.document_id)).status == "failed"


async def test_approved_stale_alert_anchored_on_approved_at(world):
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    world.advance(timedelta(hours=2))
    await world.approve(settled)
    world.advance(timedelta(minutes=1))
    assert (await escalate(world)).stale == []
    world.advance(timedelta(minutes=15))
    assert (await escalate(world)).stale == [(settled.document_id, "approved")]


async def foreign_draft(world, host_ref_id):
    """An open draft for the host reference, but of another merchant of the same tenant."""
    return await insert_draft(world, intent_id=None, host_ref_id=host_ref_id, merchant_id=uuid4())


async def test_intake_ignores_open_draft_of_another_merchant(world):
    host_ref_id = f"order-{uuid4()}"
    draft_id = await foreign_draft(world, host_ref_id)
    settled = await world.settle(draft=False, host_ref_id=host_ref_id)
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.draft_id) == ("needs_draft", None)
    receipts = world.tables.settlement_receipts
    async with world.engine.connect() as conn:
        stored = await conn.scalar(
            sa.select(sa.func.count()).where(receipts.c.settlement_id == settled.settlement_id)
        )
    assert stored == 1
    assert (await draft_row(world, draft_id)).status == "open"


async def test_sweep_ignores_open_draft_of_another_merchant(world):
    settled = await world.settle(draft=False)
    draft_id = await foreign_draft(world, settled.host_ref_id)
    report = await escalate(world)
    assert (report.drafts_attached, report.errors) == ([], [])
    assert sweep_errors(world, "attach_draft") == 0
    assert (await world.doc(settled.document_id)).status == "needs_draft"
    assert (await draft_row(world, draft_id)).status == "open"
