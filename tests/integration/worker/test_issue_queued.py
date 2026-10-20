import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.fakes.fake_einvoice_provider import (
    AUTH_FAILED,
    CONFLICT,
    CRASH_BEFORE_SEND,
    QUOTA,
    TRANSIENT,
    FakeInvoice,
    failed_invoice,
    rejected,
)
from tests.fakes.recording import Crash
from tests.integration.worker.world import vn

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRepository
from invoice_module.application.config import today_vn
from invoice_module.domain.enums import DeliveryMode, DocumentStatus
from invoice_module.ports.provider import ProviderInvoice

pytestmark = pytest.mark.integration

AUTO, MANUAL = DeliveryMode.AUTO, DeliveryMode.MANUAL


def issued(post) -> date:
    return date.fromisoformat(str(post.payload["issued_date"])[:10])


async def approved_manual(world, **settle):
    settled = await world.settle(mode=MANUAL, **settle)
    await world.issue()
    await world.approve(settled)
    return settled


async def test_accepted_moves_to_pending_provider(world):
    settled = await world.settle()
    now = world.clock.now()
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "pending_provider"
    assert (doc.status, doc.tracking_code, doc.poll_count) == (
        "pending_provider",
        settled.reference,
        0,
    )
    assert doc.pending_since == now and doc.next_attempt_at == now + timedelta(seconds=3)
    assert (doc.lease_owner, doc.lease_until, doc.post_count) == (None, None, 1)
    assert len(world.provider.posts(settled.reference)) == 1


async def test_request_frozen_before_post_matches_sent_payload(world):
    settled = await world.settle()
    seen = []
    original = world.provider.create

    async def create(account, creds, request, *, before_send=None):
        seen.append((await world.doc(settled.document_id)).request)
        return await original(account, creds, request, before_send=before_send)

    world.provider.create = create
    await world.issue()
    doc = await world.doc(settled.document_id)
    [post] = world.provider.posts(settled.reference)
    assert seen == [doc.request]
    assert doc.request == world.provider.requests_received[settled.reference] == post.payload
    assert doc.request_frozen_at <= post.at and doc.request_revision == 1
    assert doc.issued_date == today_vn(world.clock.now()) == issued(post)


async def test_two_workers_claim_one_create(world, engine, inv_tables):
    settled = await world.settle()
    other = world.build()
    await asyncio.gather(world.module.issue_queued.run_batch(), other.issue_queued.run_batch())
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts()) == 1
    assert (doc.status, doc.attempts, doc.lease_generation) == ("pending_provider", 1, 1)

    # A claim held open by one session is skipped by the other without waiting, and a live
    # lease keeps it from being claimed again after the first commit.
    second = await world.settle()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = world.clock.now()
    async with sessions() as first_session, sessions() as second_session:
        holder = DocumentRepository(first_session, inv_tables)
        contender = DocumentRepository(second_session, inv_tables)
        held = await holder.claim_due((DocumentStatus.QUEUED,), now, 60, "a", 10, True)
        assert [row.id for row in held] == [second.document_id]
        skipped = await asyncio.wait_for(
            contender.claim_due((DocumentStatus.QUEUED,), now, 60, "b", 10, True), timeout=5
        )
        assert skipped == []
        await second_session.rollback()
        await first_session.commit()
        again = await contender.claim_due((DocumentStatus.QUEUED,), now, 60, "b", 10, True)
        assert again == []
        await second_session.commit()
    doc = await world.doc(second.document_id)
    assert (doc.lease_owner, doc.attempts) == ("a", 1)


async def test_409_found_success_marks_issued(world):
    settled = await world.settle()
    world.provider.seed_invoice(
        FakeInvoice(settled.reference, date(2026, 10, 20), 100, invoice_number="00000042")
    )
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.invoice_number, doc.issued_date) == (
        "issued",
        "00000042",
        date(2026, 10, 20),
    )
    assert world.provider.ops() == ["create", "find_by_reference"]


async def test_409_found_pending_moves_to_pending_provider(world):
    settled = await world.settle()
    world.provider.seed_invoice(
        FakeInvoice(settled.reference, date(2026, 10, 21), 100, state="success", pending_polls=3)
    )
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.status == "pending_provider" and doc.pending_since == world.clock.now()
    assert await world.open_reasons(settled.document_id) == []


async def test_409_not_found_fails_with_review(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CONFLICT)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "document_existed_unresolved")
    assert await world.open_reasons(settled.document_id) == ["DOCUMENT_EXISTED_UNRESOLVED"]
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details == {"provider_state": "none"}


@pytest.mark.parametrize("failed_consumes_reference", [True, "unknown"])
async def test_409_found_failed_opens_document_existed_unresolved(world, failed_consumes_reference):
    # Either way the policy is the same: no new reference, the operator reconciles at the portal.
    settled = await world.settle()
    world.provider.seed_invoice(
        FakeInvoice(settled.reference, None, None, state="failed", provider_message=None)
    )
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.reference_code) == ("failed", settled.reference)
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details == {"provider_state": "failed"}
    await world.drive(rounds=3)
    assert len(world.provider.posts()) == 1


async def test_409_found_total_mismatch_opens_lookup_mismatch(world):
    settled = await world.settle()
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 999))
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "lookup_mismatch")
    details = await world.reason_details(settled.document_id, "LOOKUP_MISMATCH")
    assert details == {"provider_total": 999, "document_total": 100}


async def test_reclaim_with_frozen_request_looks_up_before_post(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT)
    await world.issue()
    world.past_lease()
    await world.issue()
    assert world.provider.ops() == ["create", "find_by_reference", "create"]
    assert (await world.doc(settled.document_id)).status == "pending_provider"


async def test_reclaim_same_day_resends_identical_request(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT)
    await world.issue()
    world.advance(timedelta(hours=2))
    await world.issue()
    first, second = world.provider.posts(settled.reference)
    assert first.payload == second.payload
    assert (await world.doc(settled.document_id)).request_revision == 1


async def test_reclaim_next_day_review_action_blocks(world):
    await world.set_cross_day("review")
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT)
    await world.issue()
    world.clock.set(vn(2026, 10, 22, 9))
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.status == "blocked" and doc.request is not None
    assert await world.open_reasons(settled.document_id) == ["ISSUE_DATE_CROSSED_DAY"]
    assert world.provider.ops() == ["create", "find_by_reference"]


async def test_reclaim_next_day_issue_action_refreezes_with_new_date(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT)
    await world.issue()
    world.clock.set(vn(2026, 10, 22, 9))
    await world.issue()
    first, second = world.provider.posts(settled.reference)
    doc = await world.doc(settled.document_id)
    assert (issued(first), issued(second)) == (date(2026, 10, 21), date(2026, 10, 22))
    assert (doc.issued_date, doc.request_revision, doc.status) == (
        date(2026, 10, 22),
        2,
        "pending_provider",
    )
    assert world.metrics.count("invoice_cross_day_total") == 1


async def _failed_record_then_retry(world, settled):
    """A create accepted then reported Failed; the operator retries after the lookup."""
    world.provider.new_invoice(settled.reference, final_state="failed", message="Synthetic")
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    world.provider.invoices[settled.reference].consumes_reference = False
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.request, doc.operator_retry_at is not None) == ("queued", None, True)


async def test_reclaim_with_authorized_resend_posts_despite_prior_failed_record(world):
    settled = await world.settle()
    await _failed_record_then_retry(world, settled)
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()
    assert (await world.doc(settled.document_id)).request_authorized_resend is True
    world.past_lease()
    world.restart()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.status == "pending_provider" and doc.request_authorized_resend is False
    assert len(world.provider.posts(settled.reference)) == 2
    assert world.provider.ops()[-2:] == ["find_by_reference", "create"]


@pytest.mark.parametrize("action", ["issue", "review"])
async def test_authorized_resend_across_midnight_applies_date_policy(world, action):
    await world.set_cross_day(action)
    settled = await world.settle()
    await _failed_record_then_retry(world, settled)
    world.provider.script_create(settled.reference, CRASH_BEFORE_SEND)
    with pytest.raises(Crash):
        await world.issue()
    world.clock.set(vn(2026, 10, 22, 8))
    world.restart()
    await world.issue()
    doc = await world.doc(settled.document_id)
    posts = world.provider.posts(settled.reference)
    if action == "issue":
        assert len(posts) == 2 and issued(posts[-1]) == date(2026, 10, 22)
        assert doc.status == "pending_provider"
    else:
        assert len(posts) == 1
        assert (doc.status, doc.request_authorized_resend) == ("blocked", True)
        assert "ISSUE_DATE_CROSSED_DAY" in await world.open_reasons(settled.document_id)


@pytest.mark.parametrize("outcome", [QUOTA, TRANSIENT])
async def test_authorized_resend_gets_one_granted_post_across_quota_and_transient(world, outcome):
    settled = await world.settle()
    await _failed_record_then_retry(world, settled)
    doc = await world.doc(settled.document_id)
    assert (doc.post_count, doc.post_limit) == (1, 2)
    world.provider.script_create(settled.reference, outcome)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.request_authorized_resend is True and doc.post_count == 2
    for _ in range(3):
        world.advance(timedelta(hours=2))
        await world.issue()
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts(settled.reference)) == 2
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    details = await world.reason_details(settled.document_id, "MAX_ATTEMPTS_EXCEEDED")
    assert details["lookup_state"] == "failed" and details["post_count"] == 2


async def test_post_skipped_when_lease_would_expire_before_timeout(world):
    settled = await world.settle()
    world.provider.delay_before_send(settled.reference, timedelta(seconds=45))
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "skipped:lease" and world.provider.posts() == []
    assert (doc.status, doc.post_count, doc.next_attempt_at) == (
        "queued",
        0,
        world.clock.now(),
    )
    assert world.metrics.count("invoice_post_skipped_total", reason="lease") == 1
    await world.issue()
    assert world.provider.ops() == ["find_by_reference", "create"]


async def test_post_skipped_when_operator_bumped_generation(world):
    settled = await world.settle()

    async def operator_change():
        doc = await world.doc(settled.document_id)
        await world.set_columns(settled.document_id, lease_generation=doc.lease_generation + 1)

    world.provider.on_next_prepare(operator_change)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "lease_lost" and world.provider.posts() == []
    assert (doc.post_count, doc.send_started_at) == (0, None)
    assert world.metrics.count("invoice_post_skipped_total", reason="lease") == 1


async def test_transient_backs_off_then_reviews_when_post_budget_spent(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.next_attempt_at) == ("queued", world.clock.now() + timedelta(seconds=2))
    world.past_lease()
    await world.issue()
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts()) == 2
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    details = await world.reason_details(settled.document_id, "MAX_ATTEMPTS_EXCEEDED")
    assert details == {
        "post_count": 2,
        "post_limit": 2,
        "lookup_state": "none",
        "date_resend_used": False,
    }


async def test_quota_backs_off_one_hour_and_opens_review_once(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, QUOTA, QUOTA)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.attempts, doc.next_attempt_at) == (
        "queued",
        0,
        world.clock.now() + timedelta(hours=1),
    )
    world.advance(timedelta(minutes=59))
    assert await world.issue() == []
    world.advance(timedelta(minutes=2))
    await world.issue()
    cases = await world.cases(settled.document_id)
    assert len(cases) == 1
    assert [r for r in await world.reasons(settled.document_id)] == [
        ("QUOTA_EXHAUSTED", "open", None)
    ]
    assert world.metrics.count("invoice_quota_exhausted_total") == 2
    assert len(world.provider.posts()) == 2


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("validation_error", "validation_error"),
        ("registration_not_approved", "registration_not_approved"),
        ("seller_store_invalid", "seller_store_invalid"),
        ("SOMETHING_NEW", "validation_error"),
    ],
)
async def test_rejected_opens_provider_rejected_review(world, code, error):
    settled = await world.settle()
    world.provider.script_create(settled.reference, rejected(code))
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", error)
    assert await world.reason_details(settled.document_id, "PROVIDER_REJECTED") == {"code": code}


async def test_auth_failed_opens_review(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, AUTH_FAILED)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "auth_failed")
    assert await world.open_reasons(settled.document_id) == ["AUTH_FAILED"]


async def test_cross_day_review_action_blocks_without_network(world):
    await world.set_cross_day("review")
    world.clock.set(vn(2026, 10, 21, 23, 50))
    settled = await world.settle()
    world.clock.set(vn(2026, 10, 22, 0, 10))
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.request) == ("blocked", None)
    assert world.provider.calls == []
    details = await world.reason_details(settled.document_id, "ISSUE_DATE_CROSSED_DAY")
    assert (details["settled_date"], details["issue_date"]) == ("2026-10-21", "2026-10-22")


async def test_cross_day_issue_action_sends_current_date(world):
    world.clock.set(vn(2026, 10, 21, 23, 50))
    settled = await world.settle()
    world.clock.set(vn(2026, 10, 22, 0, 10))
    await world.issue()
    [post] = world.provider.posts(settled.reference)
    assert issued(post) == date(2026, 10, 22)
    assert world.metrics.count("invoice_cross_day_total") == 1


async def test_lease_lost_does_not_overwrite(world):
    settled = await world.settle()

    async def operator_change():
        doc = await world.doc(settled.document_id)
        await world.set_columns(settled.document_id, lease_generation=doc.lease_generation + 1)

    world.provider.on_next("create", operator_change)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "lease_lost"
    assert (doc.status, doc.tracking_code, doc.pending_since) == ("queued", None, None)
    assert world.metrics.count("invoice_lease_lost_total", loop="issue") == 1


async def test_reference_code_unchanged_across_retries(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, QUOTA)
    await world.drive(rounds=3, step=timedelta(hours=2))
    doc = await world.doc(settled.document_id)
    assert doc.reference_code == settled.reference
    assert {call.reference_code for call in world.provider.calls} == {settled.reference}
    assert {post.payload["reference_code"] for post in world.provider.posts()} == {
        settled.reference
    }


async def test_manual_unapproved_moves_to_awaiting_approval_without_network(world):
    settled = await world.settle(mode=MANUAL)
    for _ in range(4):
        await world.issue()
        world.past_lease()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.next_attempt_at, doc.approved_at) == ("awaiting_approval", None, None)
    assert doc.request is not None and doc.issued_date == date(2026, 10, 21)
    assert world.provider.calls == []


async def test_create_payload_always_is_draft_false_for_both_modes(world):
    auto = await world.settle(mode=AUTO)
    manual = await approved_manual(world)
    await world.issue()
    posts = world.provider.posts()
    assert {post.reference_code for post in posts} == {auto.reference, manual.reference}
    assert all(post.payload.get("is_draft", False) is False for post in posts)


async def test_approved_reclaim_looks_up_before_first_post(world):
    settled = await approved_manual(world)
    await world.issue()
    assert world.provider.ops() == ["find_by_reference", "create"]
    assert (await world.doc(settled.document_id)).status == "pending_provider"


async def test_approved_sends_one_create_with_send_day_issued_date(world):
    settled = await world.settle(mode=MANUAL)
    await world.issue()
    world.clock.set(vn(2026, 10, 23, 9, 30))
    await world.approve(settled)
    await world.issue()
    [post] = world.provider.posts()
    doc = await world.doc(settled.document_id)
    assert issued(post) == date(2026, 10, 23) == doc.issued_date
    assert doc.approved_at == vn(2026, 10, 23, 9, 30)
    assert all(reason != "ISSUE_DATE_CROSSED_DAY" for reason, *_ in await world.reasons(doc.id))


async def test_approved_transient_and_quota_keep_approved(world):
    settled = await approved_manual(world)
    world.provider.script_create(settled.reference, TRANSIENT, QUOTA)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_by) == ("approved", "accountant-a")
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_by) == ("approved", "accountant-a")
    assert await world.open_reasons(settled.document_id) == ["QUOTA_EXHAUSTED"]


async def test_approved_rejected_opens_review(world):
    settled = await approved_manual(world)
    world.provider.script_create(settled.reference, rejected("validation_error"))
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_by) == ("failed", "accountant-a")
    assert await world.open_reasons(settled.document_id) == ["PROVIDER_REJECTED"]


async def test_approved_lookup_success_finalizes_without_post(world):
    settled = await approved_manual(world)
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 100))
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.status == "issued" and world.provider.posts() == []


async def test_stale_approval_hash_returns_to_awaiting_without_network(world):
    settled = await approved_manual(world)
    before = await world.doc(settled.document_id)
    changed = dict(before.document)
    changed["notes"] = "content changed after approval"
    await world.set_columns(settled.document_id, document=changed)
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at, doc.approved_snapshot_sha256) == (
        "awaiting_approval",
        None,
        None,
    )
    assert doc.request["notes"] == "content changed after approval"
    assert doc.request_revision == before.request_revision + 1
    assert world.provider.calls == []


async def test_awaiting_approval_never_claimed(world):
    settled = await world.settle(mode=MANUAL)
    await world.issue()
    before = await world.doc(settled.document_id)
    await world.set_columns(settled.document_id, next_attempt_at=world.clock.now())
    world.past_lease()
    assert await world.issue() == []
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.lease_generation) == ("awaiting_approval", before.lease_generation)
    assert world.provider.calls == []


@pytest.mark.parametrize("mode", [AUTO, MANUAL])
async def test_send_boundary_date_guard_refreezes_instead_of_sending_stale_date(world, mode):
    world.clock.set(vn(2026, 10, 21, 23, 59, 59, 900_000))
    if mode == MANUAL:
        settled = await world.settle(mode=MANUAL)
        await world.issue()
        await world.approve(settled)
    else:
        settled = await world.settle()
    world.provider.delay_before_send(settled.reference, timedelta(milliseconds=200))
    results = await world.issue()
    assert results[-1].outcome == "skipped:date_rolled" and world.provider.posts() == []
    assert world.metrics.count("invoice_post_skipped_total", reason="date_rolled") == 1
    await world.issue()
    [post] = world.provider.posts()
    doc = await world.doc(settled.document_id)
    assert issued(post) == date(2026, 10, 22) == doc.issued_date
    assert world.provider.ops()[-2:] == ["find_by_reference", "create"]


async def test_issued_date_invalid_looks_up_refreezes_and_resends_once(world):
    world.clock.set(vn(2026, 10, 21, 23, 59, 59, 900_000))
    settled = await world.settle()
    world.provider.receive_delay(settled.reference, timedelta(milliseconds=200))
    [result] = await world.issue()
    first, second = world.provider.posts(settled.reference)
    doc = await world.doc(settled.document_id)
    assert world.provider.ops() == ["create", "find_by_reference", "create"]
    assert (issued(first), issued(second)) == (date(2026, 10, 21), date(2026, 10, 22))
    assert result.outcome == "pending_provider" and doc.status == "pending_provider"
    assert (doc.post_count, doc.date_resend_at is not None, doc.reference_code) == (
        2,
        True,
        settled.reference,
    )


async def test_second_issued_date_invalid_opens_review(world):
    settled = await world.settle()
    world.provider.script_create(
        settled.reference, rejected("issued_date_invalid"), rejected("issued_date_invalid")
    )
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "issued_date_invalid")
    assert len(world.provider.posts()) == 2
    details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
    assert details["after_date_resend"] is True


@pytest.mark.parametrize("lookup_state", ["success", "pending", "failed", "none"])
async def test_date_resend_409_always_reviews_with_lookup_evidence(world, lookup_state):
    settled = await world.settle()
    world.provider.script_create(settled.reference, rejected("issued_date_invalid"), CONFLICT)
    evidence = {
        "success": ProviderInvoice(
            settled.reference, "success", invoice_number="1", issued_date=date(2026, 10, 21)
        ),
        "pending": ProviderInvoice(settled.reference, "pending"),
        "failed": failed_invoice(settled.reference, "Synthetic refusal"),
        "none": None,
    }[lookup_state]
    world.provider.script_lookup(settled.reference, None, evidence)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.reference_code, doc.invoice_number) == (
        "failed",
        settled.reference,
        None,
    )
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details["lookup_state"] == lookup_state and details["after_date_resend"] is True
    assert ("provider_message" in details) == (lookup_state == "failed")


@pytest.mark.parametrize("message", ["Hóa đơn bị từ chối (tổng hợp)", None])
async def test_409_lookup_failed_stores_provider_message_when_present(world, message):
    settled = await world.settle()
    world.provider.seed_invoice(
        FakeInvoice(settled.reference, None, None, state="failed", provider_message=message)
    )
    await world.issue()
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    if message is None:
        assert details == {"provider_state": "failed"}
    else:
        assert details == {"provider_state": "failed", "provider_message": message}


async def test_date_rejection_after_unrecorded_post_reviews_without_resend(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, rejected("issued_date_invalid"))
    await world.issue()
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts()) == 2
    assert (doc.status, doc.last_error_code, doc.date_resend_at) == (
        "failed",
        "issued_date_invalid",
        None,
    )
    details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
    assert details == {
        "code": "issued_date_invalid",
        "date_resend_skipped": "earlier_post_unrecorded",
        "post_count": 2,
        "post_limit": 2,
    }


async def test_date_resend_marks_post_exactly_once(world, monkeypatch):
    settled = await world.settle()
    world.provider.script_create(settled.reference, rejected("issued_date_invalid"))
    grants = []
    original = DocumentRepository.mark_post

    async def recording(self, *args, **kwargs):
        granted = await original(self, *args, **kwargs)
        grants.append((kwargs.get("date_resend", False), granted))
        return granted

    monkeypatch.setattr(DocumentRepository, "mark_post", recording)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert grants == [(False, True), (True, True)]
    assert (doc.post_count, doc.status) == (2, "pending_provider")
    assert len(world.provider.posts()) == 2


@pytest.mark.parametrize("mode", [AUTO, MANUAL])
async def test_consecutive_timeouts_stop_at_two_posts(world, mode):
    if mode == MANUAL:
        settled = await approved_manual(world)
    else:
        settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT, TRANSIENT, TRANSIENT)
    for _ in range(6):
        await world.issue()
        world.advance(timedelta(minutes=10))
    doc = await world.doc(settled.document_id)
    assert len(world.provider.posts(settled.reference)) == 2
    assert (doc.status, doc.last_error_code, doc.post_count) == (
        "failed",
        "max_attempts_exceeded",
        2,
    )


async def test_lookup_draft_under_our_reference_opens_review(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT)
    world.provider.script_lookup(settled.reference, ProviderInvoice(settled.reference, "draft"))
    await world.issue()
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "document_existed_unresolved")
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details == {"provider_state": "draft"}
    assert len(world.provider.posts()) == 1


async def test_manual_finalize_without_valid_approval_opens_review(world):
    settled = await approved_manual(world)
    await world.issue()
    await world.set_columns(settled.document_id, approved_snapshot_sha256="0" * 64)
    world.advance(timedelta(seconds=5))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.invoice_number) == ("failed", None)
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details["provider_state"] == "issued_without_approval"

    assert len(world.provider.posts()) == 1
