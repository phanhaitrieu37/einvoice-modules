import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from tests.fakes.fake_einvoice_provider import (
    AUTH_FAILED_BEFORE_SEND,
    BLOCK,
    CONFLICT,
    CRASH_AFTER_MARK,
    CRASH_AFTER_RECEIVE,
    CRASH_BEFORE_SEND,
    QUOTA,
    TRANSIENT,
    FakeInvoice,
    failed_invoice,
    rejected,
)
from tests.fakes.recording import Crash
from tests.integration.worker.world import TENANT, vn

from invoice_module.application import resolve_review as resolve_module
from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.application.resolve_review import AmountMismatch
from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.domain.document import Buyer, SettledPayment
from invoice_module.domain.enums import DeliveryMode, Environment
from invoice_module.domain.errors import (
    DocumentInFlight,
    InvalidLine,
    InvalidQuantity,
    InvalidTaxRate,
    ReferenceStillExists,
    ResolutionNotAllowed,
    ScopeMismatch,
)

pytestmark = pytest.mark.integration

AUTO, MANUAL = DeliveryMode.AUTO, DeliveryMode.MANUAL
NOTE = "Checked at the provider portal by the operator"
DATE_400 = rejected("issued_date_invalid")


def issued(post) -> date:
    return date.fromisoformat(str(post.payload["issued_date"])[:10])


async def settled_in(world, mode=AUTO, **settle):
    settled = await world.settle(mode=mode, **settle)
    if mode == MANUAL:
        await world.issue()
        await world.approve(settled)
    return settled


async def failed_by(world, outcome, mode=AUTO):
    settled = await settled_in(world, mode)
    world.provider.script_create(settled.reference, outcome)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.status == "failed", doc.status
    return settled


async def failed_after_async_failure(world, mode=AUTO):
    """Accepted, then the job ended Failed; the record does not consume the reference."""
    settled = await settled_in(world, mode)
    world.provider.new_invoice(settled.reference, final_state="failed", message="Synthetic")
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    world.provider.invoices[settled.reference].consumes_reference = False
    assert (await world.doc(settled.document_id)).status == "failed"
    return settled


async def failed_after_date_reviews(world, mode=AUTO):
    settled = await settled_in(world, mode)
    world.provider.script_create(settled.reference, DATE_400, DATE_400)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.post_count, doc.date_resend_at is not None) == ("failed", 2, True)
    return settled


async def run_claims(world, times=4, step=timedelta(hours=2)):
    for _ in range(times):
        world.advance(step)
        try:
            await world.issue()
        except Crash:
            world.restart()


async def duplicate(world, *, second_amount=100, second_draft=None):
    """A second settlement for an order that already has an invoice."""
    first = await world.settle()
    intent = uuid4()
    if second_draft is not None:
        await world.module.submit_draft.execute(
            world.draft_command(first.host_ref_id, intent, amount=second_draft)
        )
    second = await world.settle_again(first, amount=second_amount, intent_id=intent)
    assert "DUPLICATE_SETTLEMENT" in await world.open_reasons(second.document_id)
    return first, second


async def set_account_status(world, status):
    table = world.tables.issuer_accounts
    async with world.engine.begin() as conn:
        await conn.execute(
            table.update().where(table.c.id == world.account_id).values(status=status)
        )


async def draft_row(world, host_ref_id):
    table = world.tables.invoice_drafts
    async with world.engine.connect() as conn:
        return (
            await conn.execute(sa.select(table).where(table.c.host_ref_id == host_ref_id))
        ).one()


def calls_for(world, reference):
    return [call for call in world.provider.calls if call.reference_code == reference]


async def test_retry_calls_find_by_reference_first(world):
    settled = await failed_by(world, rejected("validation_error"))
    before = len(world.provider.calls)
    await world.resolve(settled.document_id, "retry")
    assert world.provider.ops()[before:] == ["find_by_reference"]


async def test_retry_found_issued_marks_issued_without_post(world):
    settled = await failed_by(world, CONFLICT)
    world.provider.seed_invoice(
        FakeInvoice(settled.reference, date(2026, 10, 21), 100, invoice_number="00000077")
    )
    result = await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (result.document_status, doc.status, doc.invoice_number) == (
        "issued",
        "issued",
        "00000077",
    )
    assert len(world.provider.posts()) == 1
    [case] = await world.cases(settled.document_id)
    assert (case.status, case.resolved_by) == ("resolved", "operator-a")


async def test_retry_found_pending_moves_failed_to_pending_provider(world):
    settled = await failed_by(world, CONFLICT)
    world.provider.seed_invoice(
        FakeInvoice(settled.reference, date(2026, 10, 21), 100, pending_polls=1)
    )
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.pending_since, doc.poll_count) == (
        "pending_provider",
        world.clock.now(),
        0,
    )


async def test_retry_none_requeues_same_reference_keeping_request(world):
    settled = await failed_by(world, rejected("validation_error"))
    before = await world.doc(settled.document_id)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.reference_code, doc.request, doc.attempts) == (
        "queued",
        settled.reference,
        before.request,
        0,
    )
    assert (doc.post_limit, doc.queued_at) == (before.post_count + 1, world.clock.now())
    [case] = await world.cases(settled.document_id)
    assert case.details["previous_attempts"][0]["attempts"] == before.attempts


async def test_retry_after_lookup_failed_clears_request_and_posts_once(world):
    settled = await failed_after_async_failure(world)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.request, doc.request_frozen_at) == ("queued", None, None)
    assert doc.operator_retry_at == world.clock.now()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts()) == 2 and doc.request_revision == 2
    assert doc.status == "pending_provider"


async def test_retry_after_max_attempts_resets_budget_and_posts_once(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT, TRANSIENT)
    await run_claims(world, times=3, step=timedelta(minutes=2))
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code, doc.attempts) == ("failed", "max_attempts_exceeded", 3)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.attempts, doc.post_limit) == ("queued", 0, 3)
    await run_claims(world, times=4, step=timedelta(minutes=2))
    assert len(world.provider.posts()) == 3
    assert (await world.doc(settled.document_id)).status == "failed"


async def test_retry_with_new_reference_refused_when_found(world):
    settled = await failed_by(world, CONFLICT)
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 100))
    with pytest.raises(ReferenceStillExists):
        await world.resolve(settled.document_id, "retry_with_new_reference", note=NOTE)
    doc = await world.doc(settled.document_id)
    assert (doc.reference_code, doc.status) == (settled.reference, "failed")


async def test_retry_with_new_reference_requires_reconciliation_note(world):
    settled = await failed_by(world, CONFLICT)
    for note in (None, "", "   ", "\t\n"):
        with pytest.raises(ValueError):
            await world.resolve(settled.document_id, "retry_with_new_reference", note=note)
    doc = await world.doc(settled.document_id)
    assert doc.reference_code == settled.reference
    assert world.provider.ops() == ["create", "find_by_reference"]
    case = await world.open_case(settled.document_id)
    table = world.tables.review_cases
    with pytest.raises(IntegrityError, match="review_cases_note_ck"):
        async with world.engine.begin() as conn:
            await conn.execute(
                table.update()
                .where(table.c.id == case.id)
                .values(
                    status="resolved",
                    resolution="retry_with_new_reference",
                    resolved_by="operator-a",
                    resolved_at=world.clock.now(),
                    resolution_note=None,
                )
            )


async def test_retry_with_new_reference_persists_before_requeue(world):
    settled = await failed_by(world, CONFLICT)
    world.crash.crash_after_next_commit()
    with pytest.raises(Crash):
        await world.resolve(settled.document_id, "retry_with_new_reference", note=NOTE)
    doc = await world.doc(settled.document_id)
    assert doc.status == "failed" and doc.reference_code != settled.reference
    assert (doc.request, doc.post_count, doc.send_started_at) == (None, 0, None)
    case = await world.open_case(settled.document_id)
    assert case.details["previous_references"][0]["reference_code"] == settled.reference
    assert "DOCUMENT_EXISTED_UNRESOLVED" in await world.open_reasons(settled.document_id)
    assert await world.issue() == []
    world.restart()
    await world.resolve(settled.document_id, "retry")
    await world.issue()
    rotated = (await world.doc(settled.document_id)).reference_code
    assert rotated == doc.reference_code
    assert [post.reference_code for post in world.provider.posts()] == [settled.reference, rotated]


async def test_retry_with_new_reference_refused_while_lease_active(world):
    world.config = InvoiceModuleConfig(issue_max_attempts=1)
    world.restart()
    settled = await world.settle()
    world.provider.script_create(settled.reference, QUOTA, BLOCK)
    await world.issue()
    case = await world.open_case(settled.document_id)
    await world.resolve(settled.document_id, "retry", reason="QUOTA_EXHAUSTED")
    worker = asyncio.create_task(world.issue())
    await asyncio.sleep(0.2)
    assert len(world.provider.posts()) == 2 and not worker.done()
    for resolution, note in (("retry_with_new_reference", NOTE), ("retry", None)):
        with pytest.raises(DocumentInFlight):
            await world.module.resolve_review.execute(case.id, resolution, "operator-a", note)
    world.provider.release(settled.reference, deliver=True, respond="timeout")
    await worker
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    world.past_lease()
    with pytest.raises(ReferenceStillExists):
        await world.resolve(
            settled.document_id,
            "retry_with_new_reference",
            note=NOTE,
            reason="MAX_ATTEMPTS_EXCEEDED",
        )
    assert (await world.doc(settled.document_id)).reference_code == settled.reference


async def test_unknown_outcome_stays_in_review_without_reconciliation(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, BLOCK, TRANSIENT)
    worker = asyncio.create_task(world.issue())
    await asyncio.sleep(0.2)
    world.advance(timedelta(minutes=2))
    world.provider.release(settled.reference, deliver=False)
    await worker
    await run_claims(world, times=2, step=timedelta(minutes=5))
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    for _ in range(24):
        world.advance(timedelta(hours=1))
        await world.module.escalate_overdue.run(world.clock.now())
        await world.drive(rounds=1)
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.reference_code) == ("failed", settled.reference)
    assert await world.open_reasons(settled.document_id) == ["MAX_ATTEMPTS_EXCEEDED"]
    assert len(world.provider.posts()) == 2


async def test_rotation_after_reconciliation_total_creates_at_most_one(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, BLOCK, CONFLICT)
    stale_worker = asyncio.create_task(world.build().issue_queued.run_batch())
    await asyncio.sleep(0.2)
    world.past_lease()
    await world.issue()
    assert (await world.doc(settled.document_id)).status == "failed"
    world.provider.reconcile_at_portal(settled.reference)
    await world.resolve(settled.document_id, "retry_with_new_reference", note=NOTE)
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    world.provider.release(settled.reference, deliver=True, respond="accepted")
    await stale_worker
    doc = await world.doc(settled.document_id)
    rotated = doc.reference_code
    assert rotated != settled.reference and doc.status == "issued"
    assert sum(world.provider.effective_creates[ref] for ref in (settled.reference, rotated)) <= 1
    assert world.provider.effective_creates[rotated] == 1
    [case] = await world.cases(settled.document_id)
    previous = [entry["reference_code"] for entry in case.details["previous_references"]]
    assert previous == [settled.reference]


async def test_operator_change_invalidates_stale_worker_cas(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, BLOCK)
    worker = asyncio.create_task(world.issue())
    await asyncio.sleep(0.2)
    assert len(world.provider.posts()) == 1 and not worker.done()
    async with world.crash() as uow:
        # The sweep opens this reason on a document in any status.
        await uow.review_cases.open_once(
            TENANT, settled.document_id, "STALE_REFERENCE_FOUND", {"previous_reference": "x"}
        )
        await uow.commit()
    world.past_lease()
    await world.resolve(settled.document_id, "acknowledge", note="Checked at the portal")
    world.provider.release(settled.reference, deliver=True, respond="accepted")
    [result] = await worker
    doc = await world.doc(settled.document_id)
    assert result.outcome == "lease_lost"
    assert (doc.status, doc.tracking_code) == ("queued", None)
    assert world.metrics.count("invoice_lease_lost_total", loop="issue") == 1


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("draft", ["missing", "matching", "mismatch"])
async def test_retry_resolves_issuer_account_unavailable_matrix(world, draft, held):
    if held:
        first = await world.settle()
        await set_account_status(world, "disabled")
        second = await world.settle_again(first, intent_id=uuid4())
    else:
        await set_account_status(world, "disabled")
        second = await world.settle(draft=False)
    if draft != "missing":
        amount = 100 if draft == "matching" else 150
        await world.module.submit_draft.execute(
            world.draft_command(second.host_ref_id, second.intent_id, amount=amount)
        )
    assert "ISSUER_ACCOUNT_UNAVAILABLE" in await world.open_reasons(second.document_id)
    await set_account_status(world, "active")
    await world.resolve(second.document_id, "retry", reason="ISSUER_ACCOUNT_UNAVAILABLE")
    doc = await world.doc(second.document_id)
    open_reasons = sorted(await world.open_reasons(second.document_id))
    assert doc.issuer_account_id == world.account_id
    if held:
        assert doc.status == "blocked" and "DUPLICATE_SETTLEMENT" in open_reasons
        assert (doc.document is not None) == (draft == "matching")
    else:
        expected = {"missing": "needs_draft", "matching": "queued", "mismatch": "blocked"}[draft]
        assert doc.status == expected
        assert open_reasons == (["AMOUNT_MISMATCH"] if draft == "mismatch" else [])


async def test_retry_acknowledges_cross_day(world):
    await world.set_cross_day("review")
    world.clock.set(vn(2026, 10, 21, 23, 50))
    settled = await world.settle()
    world.clock.set(vn(2026, 10, 22, 0, 10))
    await world.issue()
    assert (await world.doc(settled.document_id)).status == "blocked"
    await world.resolve(settled.document_id, "retry")
    assert (await world.doc(settled.document_id)).status == "queued"
    await world.issue()
    [post] = world.provider.posts()
    assert issued(post) == date(2026, 10, 22)
    assert await world.reasons(settled.document_id) == [
        ("ISSUE_DATE_CROSSED_DAY", "cleared", "retry")
    ]


async def test_mark_external_requires_numbers(world):
    settled = await failed_by(world, CONFLICT)
    for number in (None, "", "  "):
        with pytest.raises(ValueError):
            await world.resolve(
                settled.document_id,
                "mark_external",
                invoice_number=number,
                issued_date=date(2026, 10, 21),
            )
    assert (await world.doc(settled.document_id)).status == "failed"
    # A live invoice also needs the tax authority code.
    live_account = uuid4()
    await world.seed_account(
        id=live_account, environment="live", sandbox_evidence_ref="synthetic-evidence"
    )
    live_settlement = uuid4()
    host_ref = f"order-{uuid4()}"
    intent = uuid4()
    await world.module.submit_draft.execute(world.draft_command(host_ref, intent))
    recorded = await world.module.record_settled_payment.execute(
        SettledPayment(
            event_id=uuid4(),
            tenant_id=TENANT,
            merchant_id=world.merchant_id,
            environment=Environment.LIVE,
            intent_id=intent,
            settlement_id=live_settlement,
            transaction_id=uuid4(),
            amount_vnd=100,
            settled_at=world.clock.now(),
            origin="auto",
            host_ref_type="order",
            host_ref_id=host_ref,
        )
    )
    world.provider.script_create(str(live_settlement), CONFLICT)
    await world.issue()
    with pytest.raises(ValueError, match="tax authority"):
        await world.resolve(
            recorded.document_id,
            "mark_external",
            invoice_number="00000009",
            issued_date=date(2026, 10, 21),
        )
    await world.resolve(
        recorded.document_id,
        "mark_external",
        invoice_number="00000009",
        issued_date=date(2026, 10, 21),
        tax_authority_code="SYNTHETIC-CQT",
    )
    doc = await world.doc(recorded.document_id)
    assert (doc.status, doc.tax_authority_code) == ("issued", "SYNTHETIC-CQT")


async def test_attach_draft_resolves_needs_draft(world):
    settled = await world.settle(draft=False)
    world.advance(timedelta(hours=2))
    await world.module.escalate_overdue.run(world.clock.now())
    await world.resolve(
        settled.document_id,
        "attach_draft",
        draft=world.draft_command(settled.host_ref_id, settled.intent_id),
    )
    doc = await world.doc(settled.document_id)
    [case] = await world.cases(settled.document_id)
    assert doc.status == "queued" and case.status == "resolved"
    [item] = case.details["reasons"]
    assert (item["resolution"], item["cleared_by"]) == ("attach_draft", "operator-a")


async def test_attach_draft_on_failed_repairs_only_this_document(world):
    first = await failed_by(world, rejected("validation_error"))
    first_doc = await world.doc(first.document_id)
    other = await world.settle()
    await world.set_columns(other.document_id, draft_id=first_doc.draft_id)
    other_before = await world.doc(other.document_id)
    draft_before = await draft_row(world, first.host_ref_id)
    await world.resolve(
        first.document_id,
        "attach_draft",
        draft=world.draft_command(first.host_ref_id, first.intent_id, name="Corrected service"),
    )
    doc = await world.doc(first.document_id)
    assert (doc.status, doc.request, doc.draft_id) == ("queued", None, first_doc.draft_id)
    assert doc.document["lines"][0]["name"] == "Corrected service"
    assert await world.doc(other.document_id) == other_before
    assert await draft_row(world, first.host_ref_id) == draft_before
    [case] = await world.cases(first.document_id)
    assert case.details["repairs"][0]["actor"] == "operator-a"


async def test_repair_failed_document_does_not_touch_reopened_draft(world):
    first = await failed_by(world, rejected("validation_error"))
    second_intent = uuid4()
    await world.module.submit_draft.execute(
        world.draft_command(first.host_ref_id, second_intent, name="Content B")
    )
    reopened = await draft_row(world, first.host_ref_id)
    assert (reopened.status, reopened.intent_id) == ("open", second_intent)
    await world.resolve(
        first.document_id,
        "attach_draft",
        draft=world.draft_command(first.host_ref_id, first.intent_id, name="Content A2"),
    )
    assert await draft_row(world, first.host_ref_id) == reopened
    second = await world.settle_again(first, intent_id=second_intent)
    second_doc = await world.doc(second.document_id)
    assert second_doc.document["lines"][0]["name"] == "Content B"
    assert (await world.doc(first.document_id)).document["lines"][0]["name"] == "Content A2"
    after = await draft_row(world, first.host_ref_id)
    assert (after.intent_id, after.revision) == (second_intent, reopened.revision)


async def test_attach_draft_on_failed_rejects_total_not_equal_settled(world):
    settled = await failed_by(world, rejected("validation_error"))
    before = await world.doc(settled.document_id)
    with pytest.raises(AmountMismatch):
        await world.resolve(
            settled.document_id,
            "attach_draft",
            draft=world.draft_command(settled.host_ref_id, settled.intent_id, amount=150),
        )
    assert await world.doc(settled.document_id) == before


async def test_attach_draft_keeps_duplicate_settlement_reason_open(world):
    _, second = await duplicate(world, second_amount=150, second_draft=100)
    assert sorted(await world.open_reasons(second.document_id)) == [
        "AMOUNT_MISMATCH",
        "DUPLICATE_SETTLEMENT",
    ]
    await world.resolve(
        second.document_id,
        "attach_draft",
        reason="AMOUNT_MISMATCH",
        draft=world.draft_command(second.host_ref_id, second.intent_id, amount=150),
    )
    doc = await world.doc(second.document_id)
    case = await world.open_case(second.document_id)
    assert (doc.status, doc.total_vnd, case.reason) == ("blocked", 150, "DUPLICATE_SETTLEMENT")


async def test_duplicate_retry_requires_note_and_queues_frozen_snapshot(world):
    _, second = await duplicate(world, second_draft=100)
    doc = await world.doc(second.document_id)
    assert (doc.status, doc.total_vnd) == ("blocked", 100)
    for note in (None, "  "):
        with pytest.raises(ValueError):
            await world.resolve(second.document_id, "retry", note=note)
    assert (await world.doc(second.document_id)).status == "blocked"
    await world.resolve(second.document_id, "retry", note="Two separate orders confirmed")
    doc = await world.doc(second.document_id)
    assert (doc.status, doc.queued_at) == ("queued", world.clock.now())
    await world.issue()
    assert len(world.provider.posts(second.reference)) == 1


@pytest.mark.parametrize("draft", ["missing", "matching", "mismatch"])
async def test_duplicate_retry_without_snapshot_reevaluates_draft(world, draft, monkeypatch):
    _, second = await duplicate(world)
    assert (await world.doc(second.document_id)).document is None
    if draft != "missing":
        await world.module.submit_draft.execute(
            world.draft_command(
                second.host_ref_id, second.intent_id, amount=100 if draft == "matching" else 150
            )
        )
    order = []
    find, attach = resolve_module.find_draft_for_document, resolve_module.attach_draft_to_documents

    async def recording_find(uow, document):
        order.append(("find", (await world_status(uow, document.id))))
        return await find(uow, document)

    async def recording_attach(uow, draft_row, lookup, now, documents=None, **kwargs):
        order.append(
            ("attach", [doc.id for doc in documents], await world_status(uow, documents[0].id))
        )
        return await attach(uow, draft_row, lookup, now, documents=documents, **kwargs)

    async def world_status(uow, document_id):
        return (await uow.documents.get(document_id)).status.value

    monkeypatch.setattr(resolve_module, "find_draft_for_document", recording_find)
    monkeypatch.setattr(resolve_module, "attach_draft_to_documents", recording_attach)
    await world.resolve(second.document_id, "retry", note="A second order, invoice it")
    doc = await world.doc(second.document_id)
    expected = {"missing": "needs_draft", "matching": "queued", "mismatch": "blocked"}[draft]
    assert doc.status == expected
    if draft == "missing":
        assert order == [("find", "blocked")]
    else:
        assert order == [("find", "blocked"), ("attach", [second.document_id], "blocked")]
    if draft == "mismatch":
        assert await world.open_reasons(second.document_id) == ["AMOUNT_MISMATCH"]


async def test_duplicate_retry_keeps_blocked_while_other_reason_open(world):
    _, second = await duplicate(world, second_amount=150, second_draft=100)
    await world.resolve(
        second.document_id, "retry", note="Invoice it", reason="DUPLICATE_SETTLEMENT"
    )
    doc = await world.doc(second.document_id)
    case = await world.open_case(second.document_id)
    assert (doc.status, case.reason) == ("blocked", "AMOUNT_MISMATCH")
    assert await world.open_reasons(second.document_id) == ["AMOUNT_MISMATCH"]


async def test_duplicate_waive_keeps_blocked_and_never_issues(world):
    _, second = await duplicate(world, second_draft=100)
    await world.resolve(second.document_id, "waive", note="Refunded outside the module")
    await world.drive(rounds=6)
    await world.module.escalate_overdue.run(world.clock.now())
    doc = await world.doc(second.document_id)
    assert doc.status == "blocked"
    assert calls_for(world, second.reference) == []


async def test_duplicate_acknowledge_rejected(world):
    _, second = await duplicate(world)
    with pytest.raises(ResolutionNotAllowed):
        await world.resolve(second.document_id, "acknowledge", note="Seen")
    assert await world.open_reasons(second.document_id) == ["DUPLICATE_SETTLEMENT"]


@pytest.mark.parametrize("approval", ["valid", "invalid"])
async def test_retry_failed_manual_lookup_none_returns_to_approved(world, approval):
    settled = await failed_by(world, rejected("validation_error"), mode=MANUAL)
    before = await world.doc(settled.document_id)
    if approval == "invalid":
        await world.set_columns(settled.document_id, approved_snapshot_sha256="0" * 64)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    if approval == "valid":
        assert (doc.status, doc.request, doc.approved_by) == (
            "approved",
            before.request,
            "accountant-a",
        )
        await world.issue()
        assert world.provider.ops()[-2:] == ["find_by_reference", "create"]
        assert len(world.provider.posts()) == 2
    else:
        assert (doc.status, doc.approved_at) == ("queued", None)
        cases = await world.cases(settled.document_id)
        assert cases[-1].details["previous_approvals"][0]["approved_by"] == "accountant-a"
        await world.issue()
        assert (await world.doc(settled.document_id)).status == "awaiting_approval"
        assert len(world.provider.posts()) == 1


async def test_mark_external_on_manual_records_operator_as_approver(world):
    settled = await failed_by(world, CONFLICT, mode=MANUAL)
    await world.set_columns(
        settled.document_id,
        approved_at=None,
        approved_by=None,
        approved_snapshot_sha256=None,
        approved_reference_code=None,
    )
    await world.resolve(
        settled.document_id,
        "mark_external",
        actor="operator-b",
        invoice_number="00000031",
        issued_date=date(2026, 10, 21),
    )
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_by, doc.approved_reference_code) == (
        "issued",
        "operator-b",
        settled.reference,
    )
    assert doc.approved_at == world.clock.now() and doc.approved_snapshot_sha256 is not None


async def test_attach_draft_rejects_foreign_merchant_draft(world):
    settled = await world.settle(draft=False)
    world.advance(timedelta(hours=2))
    await world.module.escalate_overdue.run(world.clock.now())
    foreign = world.draft_command(settled.host_ref_id, settled.intent_id, merchant_id=uuid4())
    with pytest.raises(ScopeMismatch):
        await world.resolve(settled.document_id, "attach_draft", draft=foreign)
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.draft_id) == ("needs_draft", None)
    # The host's own submit with that draft is refused before any write as well.
    with pytest.raises(ScopeMismatch):
        await world.module.submit_draft.execute(foreign)
    async with world.engine.connect() as conn:
        table = world.tables.invoice_drafts
        count = await conn.scalar(
            sa.select(sa.func.count()).where(table.c.host_ref_id == settled.host_ref_id)
        )
    assert count == 0
    assert (await world.doc(settled.document_id)).status == "needs_draft"


async def test_discard_draft_records_actor_and_updated_at(world, caplog):
    host_ref = f"order-{uuid4()}"
    await world.module.submit_draft.execute(world.draft_command(host_ref, uuid4()))
    world.advance(timedelta(minutes=7))
    with pytest.raises(ValueError):
        await world.module.discard_draft.execute(TENANT, "order", host_ref, "  ")
    with caplog.at_level(logging.INFO):
        await world.module.discard_draft.execute(TENANT, "order", host_ref, "host-admin")
    row = await draft_row(world, host_ref)
    assert (row.status, row.updated_at) == ("discarded", world.clock.now())
    [record] = [r for r in caplog.records if r.message == "invoice_draft_discarded"]
    assert record.actor == "host-admin"
    assert world.metrics.count("invoice_drafts_discarded_total") == 1


async def test_waive_duplicate_then_retry_issuer_account_stays_blocked(world):
    first = await world.settle()
    await set_account_status(world, "disabled")
    second_intent = uuid4()
    second = await world.settle_again(first, intent_id=second_intent)
    await world.module.submit_draft.execute(world.draft_command(first.host_ref_id, second_intent))
    await world.resolve(second.document_id, "waive", note="Refunded", reason="DUPLICATE_SETTLEMENT")
    await set_account_status(world, "active")
    await world.resolve(second.document_id, "retry", reason="ISSUER_ACCOUNT_UNAVAILABLE")
    doc = await world.doc(second.document_id)
    assert doc.status == "blocked" and doc.total_vnd == 100
    await world.drive(rounds=3)
    assert calls_for(world, second.reference) == []


async def test_waive_duplicate_with_open_mismatch_then_attach_draft_stays_blocked(world):
    _, second = await duplicate(world, second_amount=150, second_draft=100)
    await world.resolve(second.document_id, "waive", note="Refunded", reason="DUPLICATE_SETTLEMENT")
    await world.resolve(
        second.document_id,
        "attach_draft",
        reason="AMOUNT_MISMATCH",
        draft=world.draft_command(second.host_ref_id, second.intent_id, amount=150),
    )
    doc = await world.doc(second.document_id)
    assert (doc.status, doc.total_vnd) == ("blocked", 150)
    await world.drive(rounds=3)
    assert calls_for(world, second.reference) == []


async def held(world, *, approved=False):
    settled = await world.settle(mode=MANUAL)
    await world.issue()
    if approved:
        await world.approve(settled)
    await world.module.hold_draft_invoice.execute(
        settled.document_id, "accountant-a", "Wrong buyer"
    )
    assert (await world.doc(settled.document_id)).status == "blocked"
    return settled


async def test_draft_held_retry_returns_to_awaiting_approval(world):
    settled = await held(world, approved=True)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at) == ("awaiting_approval", None)
    await world.drive(rounds=3)
    assert world.provider.calls == []
    await world.approve(settled)
    assert (await world.doc(settled.document_id)).status == "approved"


async def test_draft_held_attach_corrected_content_requires_fresh_approval(world):
    settled = await held(world, approved=True)
    draft_before = await draft_row(world, settled.host_ref_id)
    await world.resolve(
        settled.document_id,
        "attach_draft",
        draft=world.draft_command(
            settled.host_ref_id, settled.intent_id, mode=MANUAL, buyer=Buyer(name="Corrected buyer")
        ),
    )
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.request, doc.approved_at) == ("queued", None, None)
    assert doc.document["buyer"]["name"] == "Corrected buyer"
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at, doc.post_limit) == ("awaiting_approval", None, 2)
    assert doc.request["buyer"]["name"] == "Corrected buyer"
    assert await draft_row(world, settled.host_ref_id) == draft_before
    [case] = await world.cases(settled.document_id)
    assert [entry["reason"] for entry in case.details["previous_approvals"]] == ["held"]
    assert world.provider.calls == []


async def test_mark_external_requires_provider_issued_date(world):
    settled = await failed_by(world, CONFLICT)
    for value in (None, "2026-10-21", datetime(2026, 10, 21, 10)):
        with pytest.raises(ValueError, match="issue date"):
            await world.resolve(
                settled.document_id, "mark_external", invoice_number="00000001", issued_date=value
            )
    assert (await world.doc(settled.document_id)).status == "failed"


async def test_mark_external_records_operator_date_not_processing_date(world):
    settled = await failed_by(world, CONFLICT)
    world.clock.set(vn(2026, 10, 23, 15))
    await world.resolve(
        settled.document_id,
        "mark_external",
        invoice_number="00000005",
        issued_date=date(2026, 10, 22),
    )
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.issued_date, doc.invoice_number) == (
        "issued",
        date(2026, 10, 22),
        "00000005",
    )
    assert doc.issued_at == vn(2026, 10, 23, 15)


@pytest.mark.parametrize("variant", ["rejected_again", "reference_consumed"])
@pytest.mark.parametrize("mode", [AUTO, MANUAL])
async def test_retry_after_date_review_grants_exactly_one_post(world, mode, variant):
    settled = await failed_after_date_reviews(world, mode)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.post_limit) == ("approved" if mode == MANUAL else "queued", 3)
    world.clock.set(vn(2026, 10, 22, 9))
    world.provider.script_create(
        settled.reference, DATE_400 if variant == "rejected_again" else CONFLICT
    )
    await world.issue()
    await run_claims(world, times=3)
    doc = await world.doc(settled.document_id)
    posts = world.provider.posts(settled.reference)
    assert len(posts) == 3 and issued(posts[-1]) == date(2026, 10, 22)
    assert (doc.post_count, doc.post_limit, doc.reference_code) == (3, 3, settled.reference)
    if variant == "rejected_again":
        assert (doc.status, doc.last_error_code) == ("failed", "issued_date_invalid")
        details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
        assert details["date_resend_skipped"] == "earlier_post_unrecorded"
    else:
        assert (doc.status, doc.last_error_code) == ("failed", "document_existed_unresolved")


async def test_retry_after_date_review_survives_restart_and_posts_once(world):
    settled = await failed_after_date_reviews(world)
    await world.resolve(settled.document_id, "retry")
    world.restart()
    world.provider.script_create(settled.reference, TRANSIENT)
    await world.issue()
    assert len(world.provider.posts()) == 3
    world.restart()
    await run_claims(world, times=4)
    assert len(world.provider.posts()) == 3
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")


GRANTED_OUTCOMES = [TRANSIENT, QUOTA, CRASH_AFTER_MARK, CRASH_AFTER_RECEIVE, DATE_400]


@pytest.mark.parametrize("outcome", GRANTED_OUTCOMES, ids=str)
@pytest.mark.parametrize("lookup", ["none", "failed"])
@pytest.mark.parametrize("mode", [AUTO, MANUAL])
async def test_retry_after_unrecorded_post_date_review_grants_exactly_one_post(
    world, mode, lookup, outcome
):
    settled = await settled_in(world, mode)
    world.provider.script_create(settled.reference, TRANSIENT, DATE_400)
    await world.issue()
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.post_count, doc.date_resend_at) == ("failed", 2, None)
    if lookup == "failed":
        world.provider.script_lookup(settled.reference, failed_invoice(settled.reference, None))
    await world.resolve(settled.document_id, "retry")
    if lookup == "failed" and mode == MANUAL:
        await world.issue()
        await world.approve(settled)
    world.provider.script_create(settled.reference, outcome)
    await run_claims(world, times=5)
    doc = await world.doc(settled.document_id)
    # The grant allows one more POST; a crash right after the grant spends it unsent.
    expected_posts = 2 if outcome == CRASH_AFTER_MARK else 3
    assert len(world.provider.posts(settled.reference)) == expected_posts
    assert doc.post_count == doc.post_limit == 3
    if outcome == QUOTA:
        # The quota never clears by itself: the document waits for the operator, unclaimed.
        assert (doc.status, doc.next_attempt_at) == (
            "approved" if mode == MANUAL else "queued",
            None,
        )
        assert await world.open_reasons(settled.document_id) == ["QUOTA_EXHAUSTED"]
        return
    assert doc.status in ("failed", "pending_provider", "issued")
    if outcome != CRASH_AFTER_RECEIVE:
        assert doc.status == "failed" and await world.open_reasons(settled.document_id)
    if outcome == DATE_400:
        assert doc.last_error_code == "issued_date_invalid"


async def test_retry_after_post_budget_review_grants_exactly_one_post(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT, TRANSIENT, TRANSIENT)
    await run_claims(world, times=3, step=timedelta(minutes=2))
    assert (await world.doc(settled.document_id)).last_error_code == "max_attempts_exceeded"
    await world.resolve(settled.document_id, "retry")
    await run_claims(world, times=5, step=timedelta(minutes=2))
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts()) == 3
    assert (doc.status, doc.last_error_code, doc.post_count) == (
        "failed",
        "max_attempts_exceeded",
        3,
    )


@pytest.mark.parametrize(
    "outcome", [TRANSIENT, QUOTA, DATE_400, CRASH_AFTER_MARK, CRASH_BEFORE_SEND], ids=str
)
@pytest.mark.parametrize("mode", [AUTO, MANUAL])
async def test_retry_before_first_post_grants_exactly_one_post(world, mode, outcome):
    settled = await settled_in(world, mode)
    world.provider.script_create(settled.reference, AUTH_FAILED_BEFORE_SEND)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.post_count, doc.send_started_at) == ("failed", 0, None)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.post_count, doc.post_limit) == (0, 1)
    async with world.crash() as uow:
        await uow.documents.grant_post(settled.document_id)
        await uow.documents.grant_post(settled.document_id)
        await uow.commit()
    assert (await world.doc(settled.document_id)).post_limit == 1
    world.provider.script_create(settled.reference, outcome)
    await run_claims(world, times=5)
    doc = await world.doc(settled.document_id)
    # A crash after the grant spent it without a POST; a crash before the hook left it unused.
    expected_posts = 0 if outcome == CRASH_AFTER_MARK else 1
    assert len(world.provider.posts(settled.reference)) == expected_posts
    assert doc.post_count <= 1
    if outcome == DATE_400:
        details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
        assert details["date_resend_skipped"] == "operator_grant_used"
    if outcome in (TRANSIENT, CRASH_AFTER_MARK):
        assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    if outcome == QUOTA:
        assert (doc.status, doc.last_error_code, doc.next_attempt_at) == (
            "approved" if mode == MANUAL else "queued",
            "quota_exhausted",
            None,
        )
        assert await world.open_reasons(settled.document_id) == ["QUOTA_EXHAUSTED"]


async def test_attach_draft_on_failed_grants_exactly_one_post(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, rejected("validation_error"))
    await world.issue()
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.post_count, doc.post_limit) == ("failed", 2, 2)
    await world.resolve(
        settled.document_id,
        "attach_draft",
        draft=world.draft_command(settled.host_ref_id, settled.intent_id, name="Corrected"),
    )
    doc = await world.doc(settled.document_id)
    assert (doc.post_count, doc.post_limit) == (2, 3)
    world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT)
    await run_claims(world, times=4, step=timedelta(minutes=5))
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts()) == 3
    assert world.provider.posts()[-1].payload["items"][0]["item_name"] == "Corrected"
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    # A held draft was never sent: its correction keeps the default budget.
    held_doc = await held(world)
    await world.resolve(
        held_doc.document_id,
        "attach_draft",
        draft=world.draft_command(held_doc.host_ref_id, held_doc.intent_id, mode=MANUAL),
    )
    assert (await world.doc(held_doc.document_id)).post_limit == 2


async def test_waive_note_kept_on_reason_when_other_reason_open(world):
    _, second = await duplicate(world, second_amount=150, second_draft=100)
    await world.resolve(
        second.document_id, "waive", note="Refunded by bank transfer", reason="DUPLICATE_SETTLEMENT"
    )
    case = await world.open_case(second.document_id)
    [item] = [i for i in case.details["reasons"] if i["reason"] == "DUPLICATE_SETTLEMENT"]
    assert (item["note"], item["status"], item["resolution"]) == (
        "Refunded by bank transfer",
        "cleared",
        "waive",
    )
    await world.resolve(
        second.document_id,
        "attach_draft",
        reason="AMOUNT_MISMATCH",
        draft=world.draft_command(second.host_ref_id, second.intent_id, amount=150),
    )
    [case] = [c for c in await world.cases(second.document_id) if c.id == case.id]
    assert case.status == "resolved"
    [item] = [i for i in case.details["reasons"] if i["reason"] == "DUPLICATE_SETTLEMENT"]
    assert item["note"] == "Refunded by bank transfer"


@pytest.mark.parametrize("resolution", ["retry", "waive"])
async def test_resolve_review_takes_host_ref_lock_before_document_lock(world, resolution):
    _, second = await duplicate(world, second_draft=None)
    case = await world.open_case(second.document_id)
    key = f"{TENANT}:order:{second.host_ref_id}"
    note = "Second order confirmed" if resolution == "retry" else "Refunded"
    async with world.engine.connect() as holder:
        await holder.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
        task = asyncio.create_task(
            world.module.resolve_review.execute(case.id, resolution, "operator-a", note)
        )
        waiting = False
        for _ in range(100):
            await asyncio.sleep(0.05)
            async with world.engine.connect() as probe:
                waiting = bool(
                    await probe.scalar(
                        sa.text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND NOT granted"
                        )
                    )
                )
            if waiting:
                break
        assert waiting and not task.done()
        # The resolution waits on the host_ref lock without holding the document row.
        async with world.engine.connect() as probe:
            table = world.tables.documents
            await probe.execute(
                sa.select(table.c.id)
                .where(table.c.id == second.document_id)
                .with_for_update(nowait=True)
            )
            await probe.rollback()
        await holder.rollback()
    await task
    doc = await world.doc(second.document_id)
    if resolution == "waive":
        await world.module.submit_draft.execute(
            world.draft_command(second.host_ref_id, second.intent_id)
        )
        doc = await world.doc(second.document_id)
        assert doc.status == "blocked"
        [item] = [i for c in await world.cases(second.document_id) for i in c.details["reasons"]]
        assert item["note"] == "Refunded"
    else:
        assert doc.status == "needs_draft"


async def test_acknowledge_stale_reference_closes_case_on_issued(world):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    async with world.crash() as uow:
        await uow.review_cases.open_once(
            TENANT,
            settled.document_id,
            "STALE_REFERENCE_FOUND",
            {"previous_reference": "x"},
            world.clock.now(),
        )
        await uow.commit()
    await world.resolve(settled.document_id, "acknowledge", note="Handled at the portal")
    [case] = await world.cases(settled.document_id)
    assert (case.status, case.resolution, case.resolution_note, case.reason) == (
        "resolved",
        "acknowledge",
        "Handled at the portal",
        "STALE_REFERENCE_FOUND",
    )
    assert (await world.doc(settled.document_id)).status == "issued"


async def test_multi_reason_clear_records_actor_and_note_in_details(world):
    _, second = await duplicate(world, second_amount=150, second_draft=100)
    with pytest.raises(ValueError):
        await world.resolve(second.document_id, "waive", note=" ", reason="AMOUNT_MISMATCH")
    case = await world.open_case(second.document_id)
    assert all(item["status"] == "open" for item in case.details["reasons"])
    await world.resolve(
        second.document_id, "waive", actor="operator-c", note="No invoice", reason="AMOUNT_MISMATCH"
    )
    case = await world.open_case(second.document_id)
    [item] = [i for i in case.details["reasons"] if i["status"] == "cleared"]
    assert (item["cleared_by"], item["note"], item["resolution"]) == (
        "operator-c",
        "No invoice",
        "waive",
    )
    assert case.reason == "DUPLICATE_SETTLEMENT"


async def test_rotation_with_open_duplicate_records_reconciliation_audit(world):
    settled = await failed_by(world, CONFLICT)
    async with world.crash() as uow:
        await uow.review_cases.open_once(
            TENANT, settled.document_id, "DUPLICATE_SETTLEMENT", {}, world.clock.now()
        )
        await uow.commit()
    await world.resolve(
        settled.document_id,
        "retry_with_new_reference",
        actor="operator-d",
        note=NOTE,
        reason="DOCUMENT_EXISTED_UNRESOLVED",
    )
    case = await world.open_case(settled.document_id)
    entry = case.details["previous_references"][-1]
    assert (entry["reference_code"], entry["reconciled_by"], entry["reconciliation_note"]) == (
        settled.reference,
        "operator-d",
        NOTE,
    )
    assert await world.open_reasons(settled.document_id) == ["DUPLICATE_SETTLEMENT"]
    doc = await world.doc(settled.document_id)
    assert doc.status == "failed" and doc.reference_code != settled.reference


async def test_waive_requires_note_and_keeps_status(world):
    settled = await failed_by(world, CONFLICT)
    with pytest.raises(ValueError):
        await world.resolve(settled.document_id, "waive", note="")
    await world.resolve(settled.document_id, "waive", note="Written off with accounting")
    doc = await world.doc(settled.document_id)
    [case] = await world.cases(settled.document_id)
    assert doc.status == "failed"
    assert (case.status, case.resolution, case.resolution_note) == (
        "resolved",
        "waive",
        "Written off with accounting",
    )


async def test_waive_refused_on_issued_document(world):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    async with world.crash() as uow:
        await uow.review_cases.open_once(
            TENANT, settled.document_id, "STALE_REFERENCE_FOUND", {}, world.clock.now()
        )
        await uow.commit()
    with pytest.raises(ResolutionNotAllowed):
        await world.resolve(settled.document_id, "waive", note="Should not apply")
    assert (await world.doc(settled.document_id)).status == "issued"
    assert await world.open_reasons(settled.document_id) == ["STALE_REFERENCE_FOUND"]


async def test_resolution_outside_matrix_rejected(world):
    missing = await world.settle(draft=False)
    world.advance(timedelta(hours=2))
    await world.module.escalate_overdue.run(world.clock.now())
    with pytest.raises(ResolutionNotAllowed):
        await world.resolve(missing.document_id, "retry")
    _, second = await duplicate(world)
    for resolution, kwargs in (
        ("mark_external", {"invoice_number": "1", "issued_date": date(2026, 10, 21)}),
        ("retry_with_new_reference", {"note": NOTE}),
    ):
        with pytest.raises(ResolutionNotAllowed):
            await world.resolve(second.document_id, resolution, **kwargs)
    with pytest.raises(ResolutionNotAllowed):
        await world.resolve(second.document_id, "waive", note="x", reason="AMOUNT_MISMATCH")
    assert await world.open_reasons(missing.document_id) == ["DRAFT_MISSING"]
    assert await world.open_reasons(second.document_id) == ["DUPLICATE_SETTLEMENT"]


async def test_list_open_reviews_ordered_with_reasons(world):
    documents = []
    for _ in range(3):
        settled = await world.settle(draft=False)
        world.advance(timedelta(hours=2))
        await world.module.escalate_overdue.run(world.clock.now())
        documents.append(settled.document_id)
    _, second = await duplicate(world, second_amount=150, second_draft=100)
    views = await world.module.list_open_reviews.execute(tenant_id=TENANT)
    missing = [view for view in views if view.document_id in documents]
    assert [view.document_id for view in missing] == documents
    assert [view.open_reasons for view in missing] == [("DRAFT_MISSING",)] * 3
    [last] = [view for view in views if view.document_id == second.document_id]
    assert sorted(last.open_reasons) == ["AMOUNT_MISMATCH", "DUPLICATE_SETTLEMENT"]
    assert [v.opened_at for v in views] == sorted(v.opened_at for v in views)
    assert await world.module.list_open_reviews.execute(tenant_id="other-tenant") == []


async def test_quota_retry_after_topup_grants_exactly_one_post(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, QUOTA, QUOTA)
    await world.issue()
    assert len(world.provider.posts()) == 1
    await world.resolve(settled.document_id, "retry", reason="QUOTA_EXHAUSTED")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.next_attempt_at, doc.post_count, doc.post_limit) == (
        "queued",
        world.clock.now(),
        1,
        2,
    )
    world.provider.calls.clear()
    await world.issue()
    # The quota answer created nothing, so the lookup finds nothing and one POST follows.
    assert world.provider.ops() == ["find_by_reference", "create"]
    for _ in range(12):
        world.advance(timedelta(hours=2))
        await world.drive(rounds=1)
    doc = await world.doc(settled.document_id)
    assert world.provider.ops() == ["find_by_reference", "create"]
    assert (doc.status, doc.next_attempt_at, doc.post_count, doc.post_limit) == (
        "queued",
        None,
        2,
        2,
    )
    assert await world.open_reasons(settled.document_id) == ["QUOTA_EXHAUSTED"]
    assert sorted(case.status for case in await world.cases(settled.document_id)) == [
        "open",
        "resolved",
    ]


async def test_quota_review_on_approved_keeps_approval(world):
    settled = await settled_in(world, MANUAL)
    world.provider.script_create(settled.reference, QUOTA, QUOTA)
    await world.issue()
    before = await world.doc(settled.document_id)
    approval = (
        before.approved_at,
        before.approved_by,
        before.approved_snapshot_sha256,
        before.approved_reference_code,
    )
    assert (before.status, before.next_attempt_at) == ("approved", None)
    with pytest.raises(ResolutionNotAllowed):
        await world.resolve(settled.document_id, "acknowledge", note="Quota topped up")
    world.advance(timedelta(hours=1, seconds=1))
    assert await world.issue() == []
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.next_attempt_at, doc.post_limit) == (
        "approved",
        world.clock.now(),
        doc.post_count + 1,
    )
    assert (
        doc.approved_at,
        doc.approved_by,
        doc.approved_snapshot_sha256,
        doc.approved_reference_code,
    ) == approval
    assert await world.open_reasons(settled.document_id) == []
    world.provider.calls.clear()
    await world.issue()
    assert world.provider.ops() == ["find_by_reference", "create"]
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_by) == ("approved", "accountant-a")
    assert await world.open_reasons(settled.document_id) == ["QUOTA_EXHAUSTED"]


def money_line(quantity=Decimal("1"), price=100, rate=0, **tax):
    return {"quantity": quantity, "unit_price_vnd": price, "tax_rate": rate} | tax


@pytest.mark.parametrize(
    ("line", "error"),
    [
        (money_line(rate=-2), None),
        (money_line(rate=-1), None),
        (money_line(rate=0), None),
        (money_line(price=91, rate=10, tax_amount_vnd=9), None),
        (money_line(price=91, rate=10, tax_amount_vnd=None), None),
        (money_line(rate=0, tax_amount_vnd=0), None),
        (money_line(rate=10, tax_amount_vnd=-1), InvalidLine),
        (money_line(quantity=Decimal("NaN")), InvalidQuantity),
        (money_line(quantity=Decimal("Infinity")), InvalidQuantity),
        (money_line(quantity=1.0), TypeError),
        (money_line(price=100.0), TypeError),
        (money_line(rate=7), InvalidTaxRate),
    ],
    ids=[
        "rate-2", "rate-1", "rate0", "rate10-tax9", "tax-none", "tax-zero", "tax-negative",
        "nan", "inf", "float-quantity", "float-price", "rate7",
    ],
)  # fmt: skip
async def test_repair_validates_money_edges_before_db(world, line, error):
    settled = await failed_by(world, rejected("validation_error"))
    before = await world.doc(settled.document_id)
    cmd = world.draft_command(settled.host_ref_id, settled.intent_id)
    cmd = SubmitDraftCommand(
        cmd.tenant_id,
        cmd.merchant_id,
        cmd.host_ref_type,
        cmd.host_ref_id,
        cmd.intent_id,
        ({"name": "Corrected", "unit": "each"} | line,),
        100,
        delivery_mode=cmd.delivery_mode,
    )
    if error is None:
        await world.resolve(settled.document_id, "attach_draft", draft=cmd)
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.total_vnd) == ("queued", 100)
        assert sum(item["line_total_vnd"] for item in doc.document["lines"]) == 100
    else:
        with pytest.raises(error):
            await world.resolve(settled.document_id, "attach_draft", draft=cmd)
        assert await world.doc(settled.document_id) == before


async def test_retry_failed_refused_while_duplicate_hold_active(world):
    settled = await failed_by(world, rejected("validation_error"))
    async with world.crash() as uow:
        await uow.review_cases.open_once(
            TENANT, settled.document_id, "DUPLICATE_SETTLEMENT", {}, world.clock.now()
        )
        await uow.commit()
    before = await world.doc(settled.document_id)
    with pytest.raises(ResolutionNotAllowed, match="duplicate_hold_active"):
        await world.resolve(settled.document_id, "retry", reason="PROVIDER_REJECTED")
    assert await world.doc(settled.document_id) == before
    assert sorted(await world.open_reasons(settled.document_id)) == [
        "DUPLICATE_SETTLEMENT",
        "PROVIDER_REJECTED",
    ]


@pytest.mark.parametrize("history", ["after_grant", "after_date_resend"])
async def test_rotation_resets_post_budget_after_grant_and_after_date_resend(world, history):
    if history == "after_grant":
        settled = await failed_by(world, AUTH_FAILED_BEFORE_SEND)
        await world.resolve(settled.document_id, "retry")
        assert (await world.doc(settled.document_id)).post_limit == 1
        world.provider.script_create(settled.reference, AUTH_FAILED_BEFORE_SEND)
        await world.issue()
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.post_count, doc.post_limit) == ("failed", 0, 1)
    else:
        settled = await failed_after_date_reviews(world)
    await world.resolve(settled.document_id, "retry_with_new_reference", note=NOTE)
    doc = await world.doc(settled.document_id)
    assert doc.reference_code != settled.reference
    assert (doc.post_count, doc.post_limit, doc.date_resend_at, doc.send_started_at) == (
        0,
        2,
        None,
        None,
    )
    # The new reference gets the default two automatic POSTs, no more and no fewer.
    world.provider.script_create(doc.reference_code, TRANSIENT, TRANSIENT, TRANSIENT)
    await run_claims(world, times=4, step=timedelta(minutes=10))
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts(doc.reference_code)) == 2
    assert (doc.status, doc.last_error_code, doc.post_count) == (
        "failed",
        "max_attempts_exceeded",
        2,
    )


@pytest.mark.parametrize("state", ["success", "pending"])
async def test_attach_draft_on_failed_refused_when_provider_holds_record(world, state):
    settled = await failed_by(world, rejected("validation_error"))
    before = await world.doc(settled.document_id)
    world.provider.seed_invoice(
        FakeInvoice(
            settled.reference,
            date(2026, 10, 21),
            100,
            pending_polls=5 if state == "pending" else 0,
        )
    )
    with pytest.raises(ResolutionNotAllowed, match=f"provider_record_{state}"):
        await world.resolve(
            settled.document_id,
            "attach_draft",
            draft=world.draft_command(settled.host_ref_id, settled.intent_id, name="Corrected"),
        )
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.document, doc.request, doc.lease_generation) == (
        "failed",
        before.document,
        before.request,
        before.lease_generation,
    )
    assert await world.open_reasons(settled.document_id) == ["PROVIDER_REJECTED"]
    assert len(world.provider.posts()) == 1


async def test_operator_write_fenced_when_generation_moves_during_lookup(world):
    settled = await failed_by(world, rejected("validation_error"))
    case = await world.open_case(settled.document_id)
    gate, entered = asyncio.Event(), asyncio.Event()
    real = world.provider.find_by_reference

    async def slow(account, creds, reference_code):
        entered.set()
        await gate.wait()
        return await real(account, creds, reference_code)

    world.provider.find_by_reference = slow
    first = asyncio.create_task(world.module.resolve_review.execute(case.id, "retry", "op-a"))
    await entered.wait()
    world.provider.find_by_reference = real
    # Another operator rotates meanwhile and stops after the first transaction.
    world.crash.crash_after_next_commit()
    with pytest.raises(Crash):
        await world.build().resolve_review.execute(
            case.id, "retry_with_new_reference", "op-b", NOTE
        )
    moved = await world.doc(settled.document_id)
    assert moved.reference_code != settled.reference
    gate.set()
    with pytest.raises(DocumentInFlight):
        await first
    assert await world.doc(settled.document_id) == moved
    assert await world.open_reasons(settled.document_id) == ["PROVIDER_REJECTED"]
