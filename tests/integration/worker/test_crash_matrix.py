"""Crash at every point around a POST and around every commit; the outcome always converges.

Every test ends with ``assert_converged``: exactly one original document for the settlement, at
most one invoice at the provider across every reference the document ever had, the frozen
request equal to what the provider last received, and either ``issued`` or exactly one open
review case with a reason. POSTs are counted from ``provider.calls`` (real transport requests).
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from tests.fakes.fake_einvoice_provider import (
    CRASH_AFTER_MARK,
    CRASH_AFTER_RECEIVE,
    CRASH_BEFORE_SEND,
    LOST_RESPONSE,
    QUOTA,
    TRANSIENT,
    rejected,
)
from tests.fakes.recording import Crash
from tests.integration.worker.world import TENANT, Settled, vn

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRepository
from invoice_module.domain.document import SettledPayment
from invoice_module.domain.enums import DeliveryMode, Environment

pytestmark = pytest.mark.integration

AUTO, MANUAL = DeliveryMode.AUTO, DeliveryMode.MANUAL
DATE_400 = rejected("issued_date_invalid")


def issued(post) -> date:
    return date.fromisoformat(str(post.payload["issued_date"])[:10])


def crash() -> None:
    raise Crash("process died")


async def restart(world, delta=None):
    world.advance(delta or timedelta(seconds=world.config.lease_seconds + 1))
    world.restart()


async def step(world, runner):
    """Run one worker step; a crash restarts the process past the lease."""
    try:
        await runner()
    except Crash:
        await restart(world)


async def converge(world, rounds=8, delta=timedelta(minutes=2)):
    for _ in range(rounds):
        await step(world, world.issue)
        world.advance(timedelta(seconds=5))
        await step(world, world.poll)
        await step(world, world.artifacts)
        world.advance(delta)


async def assert_converged(world, settled):
    documents = world.tables.documents
    async with world.engine.connect() as conn:
        count = await conn.scalar(
            sa.select(sa.func.count()).where(
                documents.c.tenant_id == TENANT,
                documents.c.settlement_id == settled.settlement_id,
                documents.c.kind == "original",
            )
        )
    assert count == 1
    doc = await world.doc(settled.document_id)
    cases = await world.cases(settled.document_id)
    references = {doc.reference_code} | {
        entry["reference_code"]
        for case in cases
        for entry in case.details.get("previous_references", [])
    }
    assert sum(world.provider.effective_creates[ref] for ref in references) <= 1
    if doc.reference_code in world.provider.requests_received and doc.request is not None:
        assert doc.request == world.provider.requests_received[doc.reference_code]
    open_cases = [case for case in cases if case.status == "open"]
    if doc.status != "issued":
        assert len(open_cases) == 1 and await world.open_reasons(doc.id), doc.status
    assert doc.reference_code == settled.reference or len(references) > 1
    return doc


def posts(world, settled):
    return world.provider.posts(settled.reference)


async def approved(world):
    settled = await world.settle(mode=MANUAL)
    await world.issue()
    await world.approve(settled)
    return settled


async def failed_record_retried(world):
    """A create accepted then reported Failed (not consuming the reference); operator retry."""
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, final_state="failed", message="Synthetic")
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    world.provider.invoices[settled.reference].consumes_reference = False
    await world.resolve(settled.document_id, "retry")
    return settled


async def budget_spent_then_granted(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT)
    for _ in range(3):
        await world.issue()
        world.past_lease()
    assert (await world.doc(settled.document_id)).last_error_code == "max_attempts_exceeded"
    await world.resolve(settled.document_id, "retry")
    assert (await world.doc(settled.document_id)).post_limit == 3
    return settled


async def test_crash_in_consumer_before_receipt_commit_redelivers_once(world):
    host_ref, intent = f"order-{uuid4()}", uuid4()
    await world.module.submit_draft.execute(world.draft_command(host_ref, intent))
    event = SettledPayment(
        event_id=uuid4(),
        tenant_id=TENANT,
        merchant_id=world.merchant_id,
        environment=Environment.TEST,
        intent_id=intent,
        settlement_id=uuid4(),
        transaction_id=uuid4(),
        amount_vnd=100,
        settled_at=world.clock.now(),
        origin="auto",
        host_ref_type="order",
        host_ref_id=host_ref,
    )
    world.crash.crash_before_next_commit()
    with pytest.raises(Crash):
        await world.module.record_settled_payment.execute(event)
    world.restart()
    first = await world.module.record_settled_payment.execute(event)
    again = await world.module.record_settled_payment.execute(event)
    assert (first.created, again.created, again.document_id) == (True, False, first.document_id)

    settled = Settled(first.document_id, event.settlement_id, intent, host_ref, world.merchant_id)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 1


async def test_crash_after_claim_before_freeze(world, monkeypatch):
    settled = await world.settle()
    original = DocumentRepository.freeze_request
    calls = []

    async def dying(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Crash("died before the freeze")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(DocumentRepository, "freeze_request", dying)
    with pytest.raises(Crash):
        await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.attempts, doc.request, doc.lease_owner is not None) == (1, None, True)
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 1


async def test_crash_after_freeze_before_post(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()
    assert (await world.doc(settled.document_id)).request is not None
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 1
    assert world.provider.ops()[:2] == ["find_by_reference", "create"]


async def test_provider_received_but_response_lost_recovers_via_lookup_before_resend(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CRASH_AFTER_RECEIVE)
    with pytest.raises(Crash):
        await world.issue()
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 1
    assert world.provider.effective_creates[settled.reference] == 1


async def test_provider_received_but_response_lost_recovers_via_409(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CRASH_AFTER_RECEIVE)
    world.provider.script_lookup(settled.reference, None)  # the lookup lags behind the create
    with pytest.raises(Crash):
        await world.issue()
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 2
    assert world.provider.ops()[:4] == [
        "create",
        "find_by_reference",
        "create",
        "find_by_reference",
    ]
    assert world.provider.effective_creates[settled.reference] == 1


async def test_lost_response_across_midnight_review_action_blocks(world):
    await world.set_cross_day("review")
    world.clock.set(vn(2026, 10, 21, 23, 59))
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT)
    await world.issue()
    world.clock.set(vn(2026, 10, 22, 0, 5))
    await converge(world, rounds=3)
    doc = await assert_converged(world, settled)
    assert doc.status == "blocked" and len(posts(world, settled)) == 1
    assert await world.open_reasons(settled.document_id) == ["ISSUE_DATE_CROSSED_DAY"]


async def test_lost_response_across_midnight_issue_action_finds_existing_invoice(world):
    world.clock.set(vn(2026, 10, 21, 23, 59))
    settled = await world.settle()
    world.provider.script_create(settled.reference, LOST_RESPONSE)
    await world.issue()
    world.clock.set(vn(2026, 10, 22, 0, 5))
    await converge(world, rounds=3)
    doc = await assert_converged(world, settled)
    assert (doc.status, doc.issued_date, doc.request_revision) == ("issued", date(2026, 10, 21), 1)
    assert len(posts(world, settled)) == 1


async def test_crash_after_post_before_cas_commit(world):
    settled = await world.settle()
    world.provider.on_next("create", world.crash.crash_before_next_commit)
    with pytest.raises(Crash):
        await world.issue()
    assert (await world.doc(settled.document_id)).status == "queued"
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 1


async def test_crash_after_cas_commit(world):
    settled = await world.settle()
    world.provider.on_next("create", world.crash.crash_after_next_commit)
    with pytest.raises(Crash):
        await world.issue()
    assert (await world.doc(settled.document_id)).status == "pending_provider"
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 1


async def test_crash_in_poll_after_success_before_commit(world):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    world.provider.on_next("check_create", world.crash.crash_before_next_commit)
    with pytest.raises(Crash):
        await world.poll()
    assert (await world.doc(settled.document_id)).status == "pending_provider"
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and world.provider.ops().count("check_create") == 2


async def test_crash_after_artifact_put_before_commit(world):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    world.provider.on_next("fetch_artifacts", world.crash.crash_before_next_commit)
    with pytest.raises(Crash):
        await world.artifacts()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.pdf_uri) == ("issued", None)
    # The artifact lease covers two downloads, so the crashed claim is not taken over early.
    await restart(world)
    assert await world.artifacts() == []
    world.advance(timedelta(seconds=world.config.artifact_lease_seconds))
    [result] = await world.artifacts()
    doc = await assert_converged(world, settled)
    assert result.outcome == "stored" and doc.pdf_uri is not None and doc.xml_sha256 is not None


async def test_backup_restore_replay_hits_409_not_duplicate(world):
    settled = await world.settle()
    await converge(world, rounds=2)
    assert (await world.doc(settled.document_id)).status == "issued"
    table = world.tables
    async with world.engine.begin() as conn:
        receipt = (
            await conn.execute(
                sa.select(table.settlement_receipts).where(
                    table.settlement_receipts.c.document_id == settled.document_id
                )
            )
        ).one()
        await conn.execute(
            table.settlement_receipts.delete().where(
                table.settlement_receipts.c.document_id == settled.document_id
            )
        )
        await conn.execute(
            table.review_cases.delete().where(
                table.review_cases.c.document_id == settled.document_id
            )
        )
        await conn.execute(
            table.documents.delete().where(table.documents.c.id == settled.document_id)
        )
        await conn.execute(
            table.invoice_drafts.update()
            .where(table.invoice_drafts.c.host_ref_id == settled.host_ref_id)
            .values(status="open", consumed_by_document_id=None)
        )
    replay = SettledPayment(
        event_id=receipt.event_id,
        tenant_id=TENANT,
        merchant_id=world.merchant_id,
        environment=Environment.TEST,
        intent_id=settled.intent_id,
        settlement_id=settled.settlement_id,
        transaction_id=receipt.transaction_id,
        amount_vnd=100,
        settled_at=receipt.settled_at,
        origin="auto",
        host_ref_type="order",
        host_ref_id=settled.host_ref_id,
    )
    recorded = await world.module.record_settled_payment.execute(replay)

    restored = Settled(
        recorded.document_id,
        settled.settlement_id,
        settled.intent_id,
        settled.host_ref_id,
        world.merchant_id,
    )
    await converge(world, rounds=3)
    doc = await assert_converged(world, restored)
    assert doc.status == "issued" and doc.reference_code == settled.reference
    assert world.provider.effective_creates[settled.reference] == 1
    assert len(posts(world, settled)) == 2


async def test_crash_after_freeze_before_post_with_prior_failed_record_still_posts_once(world):
    settled = await failed_record_retried(world)
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()
    assert (await world.doc(settled.document_id)).request_authorized_resend is True
    await restart(world)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.request_authorized_resend) == ("pending_provider", False)
    await converge(world)
    await assert_converged(world, settled)
    assert len(posts(world, settled)) == 2


@pytest.mark.parametrize("action", ["issue", "review"])
async def test_crash_after_freeze_before_post_with_prior_failed_record_across_midnight(
    world, action
):
    await world.set_cross_day(action)
    settled = await failed_record_retried(world)
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()
    world.clock.set(vn(2026, 10, 22, 0, 10))
    world.restart()
    await converge(world, rounds=3)
    doc = await assert_converged(world, settled)
    if action == "issue":
        assert len(posts(world, settled)) == 2 and issued(posts(world, settled)[-1]) == date(
            2026, 10, 22
        )
    else:
        assert len(posts(world, settled)) == 1
        assert (doc.status, doc.request_authorized_resend) == ("blocked", True)


async def test_crash_between_approval_commit_and_send_posts_once(world):
    settled = await world.settle(mode=MANUAL)
    await world.issue()
    world.crash.crash_after_next_commit()
    with pytest.raises(Crash):
        await world.approve(settled)
    assert (await world.doc(settled.document_id)).status == "approved"
    world.restart()
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()
    await restart(world)
    await converge(world)
    doc = await assert_converged(world, settled)
    assert doc.status == "issued" and len(posts(world, settled)) == 1


async def test_manual_create_lost_response_after_midnight_recovers_by_lookup(world):
    world.clock.set(vn(2026, 10, 21, 23, 59, 58))
    settled = await approved(world)
    world.provider.script_create(settled.reference, LOST_RESPONSE)
    await world.issue()
    world.clock.set(vn(2026, 10, 22, 0, 2))
    await converge(world, rounds=3)
    doc = await assert_converged(world, settled)
    assert (doc.status, doc.issued_date) == ("issued", date(2026, 10, 21))
    assert len(posts(world, settled)) == 1


async def test_freeze_then_midnight_before_post_refreezes_send_day(world):
    world.clock.set(vn(2026, 10, 21, 23, 59, 59))
    settled = await world.settle()
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()
    assert (await world.doc(settled.document_id)).issued_date == date(2026, 10, 21)
    world.clock.set(vn(2026, 10, 22, 0, 1, 5))
    world.restart()
    await converge(world, rounds=3)
    doc = await assert_converged(world, settled)
    [post] = posts(world, settled)
    assert issued(post) == date(2026, 10, 22) == doc.issued_date and doc.status == "issued"


async def date_rejected_once(world, *resend):
    settled = await world.settle()
    world.provider.script_create(settled.reference, DATE_400, *resend)
    return settled


async def test_date_resend_crash_after_mark_before_post_reviews_without_post(world):
    settled = await date_rejected_once(world, CRASH_AFTER_MARK)
    with pytest.raises(Crash):
        await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.post_count, doc.date_resend_at is not None) == (2, True)
    await restart(world)
    await converge(world, rounds=4)
    doc = await assert_converged(world, settled)
    assert len(posts(world, settled)) == 1
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    details = await world.reason_details(settled.document_id, "MAX_ATTEMPTS_EXCEEDED")
    assert details["date_resend_used"] is True


async def test_date_resend_crash_after_post_before_commit_reviews_without_third_post(world):
    settled = await date_rejected_once(world, TRANSIENT)
    world.provider.on_next("create", lambda: None)
    world.provider.on_next("create", crash)
    with pytest.raises(Crash):
        await world.issue()
    await restart(world)
    await converge(world, rounds=4)
    doc = await assert_converged(world, settled)
    assert len(posts(world, settled)) == 2
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")


async def test_date_resend_budget_not_restored_by_transient_quota_or_refreeze(world):
    for outcome in (TRANSIENT, QUOTA):
        settled = await date_rejected_once(world, outcome)
        await world.issue()
        for _ in range(4):
            world.advance(timedelta(hours=1, minutes=1))
            await world.issue()
        doc = await assert_converged(world, settled)
        assert len(posts(world, settled)) == 2, outcome
        assert doc.date_resend_at is not None and doc.post_count == doc.post_limit == 2
        if outcome == QUOTA:
            # The quota waits for the operator; no claim runs, so nothing can be resent.
            assert (doc.status, doc.last_error_code, doc.request_revision) == (
                "queued",
                "quota_exhausted",
                2,
            )
            assert doc.next_attempt_at is None
            continue
        assert (doc.status, doc.last_error_code, doc.request_revision) == (
            "failed",
            "max_attempts_exceeded",
            2,
        )


async def test_date_resend_across_midnight_resends_once(world):
    world.clock.set(vn(2026, 10, 21, 23, 59, 59, 900_000))
    settled = await world.settle()
    world.provider.receive_delay(settled.reference, timedelta(milliseconds=200))
    [result] = await world.issue()
    # The resend happens in the same claim, frozen with the new day; the send-boundary
    # guard never has to step in.
    sent = await world.doc(settled.document_id)
    assert result.outcome == "pending_provider"
    assert (sent.post_count, sent.date_resend_at is not None) == (2, True)
    assert world.metrics.count("invoice_post_skipped_total") == 0
    await converge(world, rounds=3)
    doc = await assert_converged(world, settled)
    first, second = posts(world, settled)
    assert (issued(first), issued(second)) == (date(2026, 10, 21), date(2026, 10, 22))
    assert (doc.status, doc.issued_date) == ("issued", date(2026, 10, 22))


async def test_date_resend_crash_after_first_400_before_lookup_caps_posts_at_two(world):
    settled = await date_rejected_once(world, DATE_400)
    world.provider.on_next("create", crash)
    with pytest.raises(Crash):
        await world.issue()
    assert world.provider.ops() == ["create"]
    await restart(world)
    await converge(world, rounds=4)
    doc = await assert_converged(world, settled)
    assert len(posts(world, settled)) == 2
    details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
    assert details["date_resend_skipped"] == "earlier_post_unrecorded"
    assert doc.date_resend_at is None


async def test_date_resend_crash_after_lookup_before_mark_commit_caps_posts_at_two(world):
    settled = await date_rejected_once(world, DATE_400)
    world.provider.on_next("find_by_reference", world.crash.crash_before_next_commit)
    with pytest.raises(Crash):
        await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.post_count, doc.request_revision) == (1, 1)
    await restart(world)
    await converge(world, rounds=4)
    doc = await assert_converged(world, settled)
    assert len(posts(world, settled)) == 2 and doc.status == "failed"
    details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
    assert (details["date_resend_skipped"], doc.date_resend_at) == ("earlier_post_unrecorded", None)


async def test_date_resend_crash_window_across_midnight_caps_posts_at_two(world):
    world.clock.set(vn(2026, 10, 21, 23, 59))
    settled = await date_rejected_once(world, DATE_400)
    world.provider.on_next("create", crash)
    with pytest.raises(Crash):
        await world.issue()
    world.clock.set(vn(2026, 10, 22, 0, 30))
    world.restart()
    await converge(world, rounds=5)
    doc = await assert_converged(world, settled)
    first, second = posts(world, settled)
    assert (issued(first), issued(second)) == (date(2026, 10, 21), date(2026, 10, 22))
    assert doc.status == "failed" and doc.post_count == 2
    details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
    assert (details["date_resend_skipped"], doc.date_resend_at) == ("earlier_post_unrecorded", None)


async def test_operator_grant_crash_before_post_reviews_without_post(world):
    settled = await budget_spent_then_granted(world)
    world.provider.script_create(settled.reference, CRASH_AFTER_MARK)
    with pytest.raises(Crash):
        await world.issue()
    assert (await world.doc(settled.document_id)).post_count == 3
    await restart(world)
    await converge(world, rounds=4)
    doc = await assert_converged(world, settled)
    assert len(posts(world, settled)) == 2
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")


async def test_operator_grant_crash_after_post_reviews_without_second_post(world):
    settled = await budget_spent_then_granted(world)
    world.provider.script_create(settled.reference, TRANSIENT)
    world.provider.on_next("create", crash)
    with pytest.raises(Crash):
        await world.issue()
    await restart(world)
    await converge(world, rounds=4)
    doc = await assert_converged(world, settled)
    assert len(posts(world, settled)) == 3
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")


async def test_operator_grant_not_reused_after_transient_or_quota(world):
    for outcome in (TRANSIENT, QUOTA):
        settled = await budget_spent_then_granted(world)
        world.provider.script_create(settled.reference, outcome)
        for _ in range(5):
            await world.issue()
            world.advance(timedelta(hours=1, minutes=1))
        doc = await assert_converged(world, settled)
        assert len(posts(world, settled)) == 3, outcome
        status = "queued" if outcome == QUOTA else "failed"
        assert (doc.status, doc.post_count, doc.post_limit) == (status, 3, 3)
        if outcome == QUOTA:
            assert doc.next_attempt_at is None


@pytest.mark.parametrize("mode", [AUTO, MANUAL])
async def test_post_budget_caps_crash_then_timeout_at_two_posts(world, mode):
    world.clock.set(vn(2026, 10, 21, 23, 40))
    settled = await (approved(world) if mode == MANUAL else world.settle())
    world.provider.script_create(settled.reference, DATE_400, CRASH_BEFORE_SEND, TRANSIENT)
    with pytest.raises(Crash):
        await world.issue()
    await restart(world)
    await world.issue()
    assert len(posts(world, settled)) == 2
    for moment in (vn(2026, 10, 21, 23, 55), vn(2026, 10, 22, 0, 30), vn(2026, 10, 22, 9)):
        world.clock.set(moment)
        world.restart()
        await converge(world, rounds=2)
    doc = await assert_converged(world, settled)
    assert len(posts(world, settled)) == 2
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")


async def test_refreeze_for_new_day_keeps_operator_resend_permission(world):
    settled = await failed_record_retried(world)
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()  # frozen with the operator's permission, died before the send
    world.clock.set(vn(2026, 10, 22, 0, 10))
    world.restart()
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()  # refrozen for the new day, died before the send again
    refrozen = await world.doc(settled.document_id)
    assert (refrozen.issued_date, refrozen.request_authorized_resend) == (date(2026, 10, 22), True)
    await restart(world)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.request_authorized_resend) == ("pending_provider", False)
    assert len(posts(world, settled)) == 2 and issued(posts(world, settled)[-1]) == date(
        2026, 10, 22
    )
    await converge(world, rounds=3)
    await assert_converged(world, settled)
    assert len(posts(world, settled)) == 2
