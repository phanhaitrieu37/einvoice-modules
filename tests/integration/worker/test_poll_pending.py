import logging
from datetime import date, timedelta

import pytest
from tests.fakes.fake_einvoice_provider import (
    AUTH_FAILED,
    TRANSIENT,
    FakeInvoice,
    check_rejected,
    failed_invoice,
)
from tests.integration.worker.world import TENANT, vn

from invoice_module.domain.enums import DeliveryMode
from invoice_module.ports.provider import ProviderInvoice, ProviderProgress

pytestmark = pytest.mark.integration

DATE_ORDER_MESSAGE = (
    "Ngày lập (07/10/2026) phải lớn hơn hoặc bằng ngày lập của hóa đơn số 330964 "
    "(20/10/2026) liền kề trước cùng ký hiệu C26TSE."
)
# A synthetic stand-in: the module reads the adapter's code, never this text.
UNCONFIRMED_MESSAGE = "Thông điệp tổng hợp: kết quả chưa rõ."


async def accepted(world, **settle):
    settled = await world.settle(**settle)
    await world.issue()
    assert (await world.doc(settled.document_id)).status == "pending_provider"
    world.advance(timedelta(seconds=3))
    return settled


async def test_success_marks_issued_with_numbers(world):
    settled = await accepted(world)
    await world.poll()
    doc = await world.doc(settled.document_id)
    invoice = world.provider.invoices[settled.reference]
    assert (doc.status, doc.invoice_number, doc.provider_invoice_id) == (
        "issued",
        invoice.invoice_number,
        invoice.provider_invoice_id,
    )
    assert doc.issued_date == date(2026, 10, 21) and doc.issued_at == world.clock.now()
    assert doc.next_attempt_at == world.clock.now()  # the artifact loop picks it up
    assert world.provider.ops() == ["create", "check_create"]


async def test_failed_opens_review(world):
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, final_state="failed", message="Synthetic")
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "provider_failed")
    assert await world.open_reasons(settled.document_id) == ["PROVIDER_REJECTED"]


async def test_pending_follows_schedule_and_counts_polls_not_attempts(world):
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, pending_polls=10_000)
    await world.issue()
    delays = []
    for _ in range(200):
        doc = await world.doc(settled.document_id)
        world.clock.set(doc.next_attempt_at)
        await world.poll()
        after = await world.doc(settled.document_id)
        delays.append((after.next_attempt_at - world.clock.now()).total_seconds())
    doc = await world.doc(settled.document_id)
    assert delays[:7] == [5, 10, 20, 30, 60, 60, 60]
    assert set(delays[5:]) == {60}
    assert (doc.status, doc.poll_count, doc.attempts) == ("pending_provider", 200, 1)
    assert world.provider.ops().count("check_create") == 200


async def test_uses_find_by_reference_without_tracking_code(world):
    settled = await accepted(world)
    await world.set_columns(settled.document_id, tracking_code=None)
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert doc.status == "issued" and doc.issued_date == date(2026, 10, 21)
    assert world.provider.ops() == ["create", "find_by_reference"]


async def test_lookup_none_without_tracking_fails_with_review(world):
    settled = await accepted(world)
    await world.set_columns(settled.document_id, tracking_code=None)
    world.provider.script_lookup(settled.reference, None)
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "document_existed_unresolved")
    assert await world.open_reasons(settled.document_id) == ["DOCUMENT_EXISTED_UNRESOLVED"]


async def test_transient_keeps_pending(world):
    settled = await accepted(world)
    world.provider.script_check(settled.reference, TRANSIENT)
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.poll_count, doc.attempts) == ("pending_provider", 0, 1)
    assert doc.next_attempt_at == world.clock.now() + timedelta(seconds=3)
    world.advance(timedelta(seconds=3))
    await world.poll()
    assert (await world.doc(settled.document_id)).status == "issued"


async def test_manual_approved_poll_uses_check_create_and_records_provider_date(world):
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    world.clock.set(vn(2026, 10, 22, 9))
    await world.approve(settled)
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert doc.status == "issued" and doc.issued_date == date(2026, 10, 22)
    assert (doc.approved_by, doc.approved_at) == ("accountant-a", vn(2026, 10, 22, 9))
    assert world.provider.ops() == ["find_by_reference", "create", "check_create"]


async def test_poll_on_later_day_records_provider_date_not_poll_date(world):
    settled = await world.settle()
    world.clock.set(vn(2026, 10, 22, 23, 59))
    world.provider.new_invoice(settled.reference, pending_polls=1)
    await world.issue()
    world.clock.set(vn(2026, 10, 23, 0, 30))
    await world.poll()
    world.advance(timedelta(minutes=1))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert doc.status == "issued" and doc.issued_date == date(2026, 10, 22)


@pytest.mark.parametrize("omitted", [("check",), ("check", "lookup")])
async def test_missing_provider_date_looks_up_then_reviews(world, omitted):
    settled = await world.settle()
    world.provider.omit_issued_date(settled.reference, *omitted)
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert world.provider.ops() == ["create", "check_create", "find_by_reference"]
    if "lookup" in omitted:
        assert (doc.status, doc.issued_date) == ("failed", date(2026, 10, 21))
        assert doc.last_error_code == "lookup_mismatch"
        details = await world.reason_details(settled.document_id, "LOOKUP_MISMATCH")
        assert details == {"missing": "issued_date"}
    else:
        assert (doc.status, doc.issued_date) == ("issued", date(2026, 10, 21))


async def test_draft_state_on_poll_opens_review(world):
    settled = await accepted(world)
    world.provider.script_check(settled.reference, ProviderProgress(state="draft"))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "document_existed_unresolved")
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details == {"provider_state": "draft"}


async def test_async_failed_maps_to_provider_rejected_with_verbatim_message(world):
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, final_state="failed", message=DATE_ORDER_MESSAGE)
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    await world.drive(rounds=4)
    doc = await world.doc(settled.document_id)
    details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
    assert details == {"provider_job_state": "Failed", "provider_message": DATE_ORDER_MESSAGE}
    assert (doc.status, doc.last_error_code, doc.reference_code) == (
        "failed",
        "provider_failed",
        settled.reference,
    )
    assert len(world.provider.posts()) == 1


async def test_no_branching_on_provider_message_text(world):
    outcomes = []
    for message in (DATE_ORDER_MESSAGE, "Thông tin người mua không hợp lệ (tổng hợp)."):
        settled = await world.settle()
        world.provider.new_invoice(settled.reference, final_state="failed", message=message)
        await world.issue()
        world.advance(timedelta(seconds=3))
        await world.poll()
        doc = await world.doc(settled.document_id)
        details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
        assert details["provider_message"] == message
        outcomes.append(
            (doc.status, doc.last_error_code, tuple(await world.reasons(doc.id)), sorted(details))
        )
    assert outcomes[0] == outcomes[1]
    await world.drive(rounds=3)
    assert len(world.provider.posts()) == 2


async def test_finalize_issued_clears_open_provider_reasons(world):
    settled = await accepted(world)
    async with world.crash() as uow:
        await uow.review_cases.open_once(
            TENANT, settled.document_id, "PROVIDER_TIMEOUT", {"poll_count": 0}, world.clock.now()
        )
        await uow.commit()
    await world.poll()
    doc = await world.doc(settled.document_id)
    [case] = await world.cases(settled.document_id)
    [entry] = case.details["reasons"]
    assert doc.status == "issued"
    assert (entry["reason"], entry["status"], entry["resolution"], entry["cleared_by"]) == (
        "PROVIDER_TIMEOUT",
        "cleared",
        "retry",
        "system:issuance",
    )
    assert case.status == "resolved"


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "success_total_mismatch",
        "pending",
        "failed",
        "cancelled",
        "none",
        "check_auth_failed",
        "lookup_auth_failed",
    ],
)
async def test_check_rejected_looks_up_before_review(world, case):
    settled = await accepted(world)
    now = world.clock.now()
    total = (await world.doc(settled.document_id)).total_vnd
    if case == "check_auth_failed":
        world.provider.script_check(settled.reference, AUTH_FAILED)
    else:
        world.provider.script_check(settled.reference, check_rejected("NOT_FOUND"))
    if case == "success_total_mismatch":
        world.provider.script_lookup(
            settled.reference,
            ProviderInvoice(
                settled.reference,
                "success",
                settled.reference,
                provider_invoice_id="inv-other",
                invoice_number="00000077",
                total_amount_vnd=total + 1,
                issued_date=date(2026, 10, 21),
            ),
        )
    elif case == "lookup_auth_failed":
        world.provider.script_lookup(settled.reference, AUTH_FAILED)
    elif case == "pending":
        world.provider.script_lookup(
            settled.reference, ProviderInvoice(settled.reference, "pending", settled.reference)
        )
    elif case == "failed":
        world.provider.script_lookup(
            settled.reference, failed_invoice(settled.reference, "Synthetic refusal")
        )
    elif case == "cancelled":
        world.provider.cancel_under_reference(settled.reference)
    elif case == "none":
        world.provider.script_lookup(settled.reference, None)
    [result] = await world.poll()
    doc = await world.doc(settled.document_id)
    ops = world.provider.ops()
    assert len(world.provider.posts()) == 1
    if case in ("check_auth_failed", "lookup_auth_failed"):
        # Both are reads: retried shortly, without a review.
        assert ops == (
            ["create", "check_create"]
            if case == "check_auth_failed"
            else ["create", "check_create", "find_by_reference"]
        )
        assert (result.outcome, doc.status, doc.tracking_code) == (
            "backoff",
            "pending_provider",
            settled.reference,
        )
        assert doc.next_attempt_at == now + timedelta(seconds=3)
        assert await world.cases(settled.document_id) == []
        return
    assert ops == ["create", "check_create", "find_by_reference"]
    if case == "success":
        assert (doc.status, doc.invoice_number) == (
            "issued",
            world.provider.invoices[settled.reference].invoice_number,
        )
        return
    if case == "pending":
        assert (doc.status, doc.tracking_code, doc.poll_count) == ("pending_provider", None, 1)
        assert doc.next_attempt_at == now + timedelta(seconds=5)
        assert await world.cases(settled.document_id) == []
        world.advance(timedelta(seconds=5))
        await world.poll()
        assert world.provider.ops()[-1] == "find_by_reference"
        assert (await world.doc(settled.document_id)).status == "issued"
        return
    if case == "success_total_mismatch":
        # The lookup is ours only if its total matches: a different invoice is never issued.
        assert (doc.status, doc.last_error_code, doc.invoice_number, doc.issued_at) == (
            "failed",
            "lookup_mismatch",
            None,
            None,
        )
        assert await world.open_reasons(settled.document_id) == ["LOOKUP_MISMATCH"]
        details = await world.reason_details(settled.document_id, "LOOKUP_MISMATCH")
        assert details == {"provider_total": total + 1, "document_total": total}
        return
    expected = {
        "failed": ("provider_failed", "PROVIDER_REJECTED"),
        "cancelled": ("document_existed_unresolved", "DOCUMENT_EXISTED_UNRESOLVED"),
        "none": ("document_existed_unresolved", "DOCUMENT_EXISTED_UNRESOLVED"),
    }[case]
    assert (doc.status, doc.last_error_code) == ("failed", expected[0])
    assert await world.open_reasons(settled.document_id) == [expected[1]]
    details = await world.reason_details(settled.document_id, expected[1])
    assert (
        details
        == {
            "failed": {"provider_job_state": "Failed", "provider_message": "Synthetic refusal"},
            "cancelled": {"provider_state": "cancelled"},
            "none": {"provider_state": "none", "phase": "poll", "check_status": "NOT_FOUND"},
        }[case]
    )


@pytest.mark.parametrize("state", ["failed", "pending", "success"])
async def test_unconfirmed_code_opens_review_without_resend(world, state):
    settled = await accepted(world)
    world.provider.unconfirmed(settled.reference, state, message=UNCONFIRMED_MESSAGE)
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code, doc.next_attempt_at) == (
        "failed",
        "provider_failed",
        None,
    )
    assert doc.invoice_number is None
    assert await world.open_reasons(settled.document_id) == ["PROVIDER_REJECTED"]
    # The adapter carries the provider's message on a failed check only.
    expected = {"unconfirmed": True, "provider_job_state": state}
    if state == "failed":
        expected["provider_message"] = UNCONFIRMED_MESSAGE
    assert await world.reason_details(settled.document_id, "PROVIDER_REJECTED") == expected
    # Neither a resend nor a wait for the timeout: the case stays with the operator.
    for _ in range(4):
        world.advance(timedelta(hours=1))
        await world.drive(rounds=1)
        await world.module.escalate_overdue.run(world.clock.now())
    assert world.provider.ops() == ["create", "check_create"]
    assert (await world.doc(settled.document_id)).status == "failed"


async def test_poll_lookup_cancelled_opens_document_existed_unresolved(world):
    settled = await accepted(world)
    await world.set_columns(settled.document_id, tracking_code=None)
    world.provider.cancel_under_reference(settled.reference)
    [result] = await world.poll()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "failed"
    assert (doc.status, doc.last_error_code, doc.invoice_number, doc.issued_at) == (
        "failed",
        "document_existed_unresolved",
        None,
        None,
    )
    assert world.provider.ops() == ["create", "find_by_reference"]
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details == {"provider_state": "cancelled"}


# Attributes every log record has; anything else on a record came in through ``extra``.
STANDARD_RECORD_KEYS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
}


async def test_worker_logs_never_include_provider_message(world, caplog):
    caplog.set_level(logging.DEBUG, logger="invoice_module")
    # Each message carries its own marker token, so any slice of it in a log is traceable.
    tokens = {
        "poll_failed": "QX7PFAILZ",
        "lookup_failed": "QX7LFAILZ",
        "conflict_lookup": "QX7CONFLZ",
        "unconfirmed": "QX7UNCONZ",
    }
    messages = {
        "poll_failed": f"Tổng hợp 1 {tokens['poll_failed']}: hoá đơn bị từ chối ở bước xử lý.",
        "lookup_failed": f"Tổng hợp 2 {tokens['lookup_failed']}: bản ghi lỗi tra theo reference",
        "conflict_lookup": f"Tổng hợp 3 {tokens['conflict_lookup']}: 409 rồi tra cứu",
        "unconfirmed": f"{UNCONFIRMED_MESSAGE} {tokens['unconfirmed']}",
    }
    documents = {}
    poll_failed = await world.settle()
    world.provider.new_invoice(
        poll_failed.reference, final_state="failed", message=messages["poll_failed"]
    )
    documents["poll_failed"] = poll_failed
    lookup_failed = await world.settle()
    world.provider.script_create(lookup_failed.reference, TRANSIENT)
    documents["lookup_failed"] = lookup_failed
    conflict = await world.settle()
    world.provider.seed_invoice(
        FakeInvoice(
            conflict.reference,
            None,
            None,
            state="failed",
            provider_message=messages["conflict_lookup"],
        )
    )
    documents["conflict_lookup"] = conflict
    unconfirmed = await world.settle()
    documents["unconfirmed"] = unconfirmed
    # An unconfirmed pending check carries no message at all.
    unconfirmed_pending = await world.settle()
    documents["unconfirmed_pending"] = unconfirmed_pending
    await world.issue()
    world.provider.script_lookup(
        lookup_failed.reference, failed_invoice(lookup_failed.reference, messages["lookup_failed"])
    )
    world.provider.unconfirmed(unconfirmed.reference, "failed", message=messages["unconfirmed"])
    world.provider.unconfirmed(unconfirmed_pending.reference, "pending", message="unused")
    world.past_lease()
    await world.issue()
    await world.poll()
    for name, settled in documents.items():
        doc = await world.doc(settled.document_id)
        assert doc.status == "failed", name
    details = await world.reason_details(unconfirmed.document_id, "PROVIDER_REJECTED")
    assert details["provider_message"] == messages["unconfirmed"]
    records = [r for r in caplog.records if r.name.startswith("invoice_module")]
    slices = {
        message[start : start + 8]
        for message in messages.values()
        for start in range(len(message) - 7)
    }
    for record in records:
        text = record.getMessage() + " ".join(str(value) for value in vars(record).values())
        assert not any(token in text for token in tokens.values()), record.msg
        assert not [piece for piece in slices if piece in text], record.msg
    failed_records = [record for record in records if record.msg == "invoice_document_failed"]
    for record in failed_records:
        assert set(vars(record)) - STANDARD_RECORD_KEYS == {
            "document_id",
            "error_code",
            "reason",
            "provider_state",
            "provider_message_length",
        }
    lengths = {record.document_id: record.provider_message_length for record in failed_records}
    assert lengths == {
        str(settled.document_id): len(messages[name]) if name in messages else None
        for name, settled in documents.items()
    }
