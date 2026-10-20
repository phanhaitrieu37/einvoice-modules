from datetime import date, timedelta

import pytest
from tests.fakes.fake_einvoice_provider import TRANSIENT
from tests.integration.worker.world import vn

from invoice_module.domain.enums import DeliveryMode
from invoice_module.ports.provider import ProviderProgress

pytestmark = pytest.mark.integration

DATE_ORDER_MESSAGE = (
    "Ngày lập (07/10/2026) phải lớn hơn hoặc bằng ngày lập của hóa đơn số 330964 "
    "(20/10/2026) liền kề trước cùng ký hiệu C26TSE."
)


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
