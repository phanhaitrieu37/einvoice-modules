"""A frozen request whose lookup finds nothing: the job is checked by reference before any resend.

The sandbox lookup did not see a create it had just accepted (404 while the create answered
409), so a lookup ``None`` is not proof that the provider never received the POST.
"""

from datetime import date, timedelta

import pytest
from tests.fakes.fake_einvoice_provider import (
    AUTH_FAILED,
    CRASH_AFTER_RECEIVE,
    LOST_RESPONSE,
    NO_JOB,
    TRANSIENT,
    check_rejected,
)
from tests.fakes.recording import Crash

from invoice_module.domain.enums import DeliveryMode
from invoice_module.ports.provider import ProviderInvoice, ProviderProgress

pytestmark = pytest.mark.integration


async def lost_create(world, outcome=LOST_RESPONSE, **new_invoice):
    """The provider received the create, the worker never saw the answer, the lease ran out."""
    settled = await world.settle()
    world.provider.script_create(settled.reference, outcome)
    if new_invoice:
        world.provider.new_invoice(settled.reference, **new_invoice)
    if outcome == CRASH_AFTER_RECEIVE:
        with pytest.raises(Crash):
            await world.issue()
        world.past_lease()
        world.restart()
    else:
        [result] = await world.issue()
        assert result.outcome == "backoff"
        world.past_lease()
    assert world.provider.ops() == ["create"]
    return settled


@pytest.mark.parametrize("outcome", [CRASH_AFTER_RECEIVE, LOST_RESPONSE])
async def test_job_found_by_check_while_lookup_is_blind_is_polled_not_resent(world, outcome):
    """The sandbox sequence: lost answer, lookup ``None``, check ``Pending``, one create."""
    settled = await lost_create(world, outcome, pending_polls=1)
    world.provider.lag_lookup(settled.reference)
    now = world.clock.now()
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert [call.op for call in world.provider.posts(settled.reference)] == ["create"]
    assert result.outcome == "pending_provider"
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert (doc.status, doc.tracking_code, doc.post_count, doc.poll_count) == (
        "pending_provider",
        settled.reference,
        1,
        0,
    )
    assert doc.pending_since == now and doc.next_attempt_at == now + timedelta(seconds=3)
    assert await world.cases(settled.document_id) == []
    world.advance(timedelta(seconds=3))
    await world.poll()
    doc = await world.doc(settled.document_id)
    invoice = world.provider.invoices[settled.reference]
    assert (doc.status, doc.invoice_number, doc.issued_date) == (
        "issued",
        invoice.invoice_number,
        date(2026, 10, 21),
    )
    assert world.provider.ops()[3:] == ["check_create"]
    assert len(world.provider.posts(settled.reference)) == 1
    assert world.provider.effective_creates[settled.reference] == 1
    # SePay reports no provider invoice id; the artifacts are fetched by reference all the same.
    assert doc.provider_invoice_id is None
    [stored] = await world.artifacts()
    doc = await world.doc(settled.document_id)
    assert stored.outcome == "stored" and doc.pdf_uri and doc.xml_uri
    assert len(world.provider.posts(settled.reference)) == 1


@pytest.mark.parametrize("state", ["success", "failed"])
async def test_finished_job_found_by_check_goes_to_the_poll_loop(world, state):
    settled = await lost_create(world, final_state=state, message="Synthetic")
    world.provider.script_lookup(settled.reference, None)
    [result] = await world.issue()
    assert result.outcome == "pending_provider"
    world.advance(timedelta(seconds=3))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert world.provider.ops() == [
        "create",
        "find_by_reference",
        "check_create",
        "check_create",
    ]
    assert len(world.provider.posts()) == 1
    if state == "success":
        assert doc.status == "issued"
    else:
        assert (doc.status, doc.last_error_code) == ("failed", "provider_failed")
        assert await world.open_reasons(settled.document_id) == ["PROVIDER_REJECTED"]


async def test_no_job_at_the_provider_resends_once(world):
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(settled.reference, NO_JOB)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "pending_provider"
    assert world.provider.ops() == ["create", "find_by_reference", "check_create", "create"]
    assert (doc.status, doc.post_count, doc.reference_code) == (
        "pending_provider",
        2,
        settled.reference,
    )
    first, second = world.provider.posts(settled.reference)
    assert first.payload == second.payload
    world.advance(timedelta(seconds=3))
    await world.poll()
    assert (await world.doc(settled.document_id)).status == "issued"
    assert len(world.provider.posts()) == 2
    assert world.provider.effective_creates[settled.reference] == 1


@pytest.mark.parametrize("answer", [TRANSIENT, check_rejected("BAD_REQUEST")])
async def test_unclear_check_backs_off_without_resending(world, answer):
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(settled.reference, answer)
    now = world.clock.now()
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "backoff"
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert (doc.status, doc.last_error_code, doc.post_count) == (
        "queued",
        "transient_error",
        1,
    )
    assert doc.next_attempt_at > now and doc.lease_owner is None
    assert await world.cases(settled.document_id) == []
    # The next claim looks and checks again; only "no such job" lets the resend through.
    world.clock.set(doc.next_attempt_at)
    await world.issue()
    assert world.provider.ops()[3:] == ["find_by_reference", "check_create", "create"]
    assert len(world.provider.posts()) == 2


async def test_check_auth_failure_opens_auth_review_without_resending(world):
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(settled.reference, AUTH_FAILED)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert (result.outcome, doc.status, doc.last_error_code) == ("failed", "failed", "auth_failed")
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert await world.reason_details(settled.document_id, "AUTH_FAILED") == {"phase": "check"}


@pytest.mark.parametrize("authorized", [False, True])
async def test_failed_job_resends_only_with_operator_permission(world, authorized):
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(settled.reference, ProviderProgress(state="failed"))
    await world.set_columns(settled.document_id, request_authorized_resend=authorized)
    [result] = await world.issue()
    ops = world.provider.ops()
    if authorized:
        assert ops == ["create", "find_by_reference", "check_create", "create"]
        assert result.outcome == "pending_provider"
    else:
        assert ops == ["create", "find_by_reference", "check_create"]
        doc = await world.doc(settled.document_id)
        assert (result.outcome, doc.tracking_code) == ("pending_provider", settled.reference)


async def test_first_send_of_an_approved_manual_draft_does_not_check(world):
    """A frozen request with no POST yet: nothing can have reached the provider."""
    settled = await world.settle(mode=DeliveryMode.MANUAL)
    await world.issue()
    await world.approve(settled)
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.post_count) == ("approved", 0) and doc.request is not None
    world.provider.calls.clear()
    [result] = await world.issue()
    assert result.outcome == "pending_provider"
    assert world.provider.ops() == ["find_by_reference", "create"]


@pytest.mark.parametrize("lookup", ["success", "none"])
async def test_poll_with_no_job_under_the_tracking_code_lets_the_lookup_decide(world, lookup):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    world.provider.script_check(settled.reference, NO_JOB)
    if lookup == "none":
        world.provider.script_lookup(settled.reference, None)
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert world.provider.ops() == ["create", "check_create", "find_by_reference"]
    if lookup == "success":
        invoice = world.provider.invoices[settled.reference]
        assert (doc.status, doc.invoice_number) == ("issued", invoice.invoice_number)
        return
    assert (doc.status, doc.last_error_code) == ("failed", "document_existed_unresolved")
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details == {"provider_state": "none", "phase": "poll", "check_status": "no_job"}


async def test_lookup_lag_hides_only_a_pending_job(world):
    settled = await world.settle()
    world.provider.new_invoice(settled.reference, pending_polls=1)
    world.provider.lag_lookup(settled.reference)
    await world.issue()
    assert await world.provider.find_by_reference(None, None, settled.reference) is None
    world.advance(timedelta(seconds=3))
    await world.poll()
    found = await world.provider.find_by_reference(None, None, settled.reference)
    assert isinstance(found, ProviderInvoice) and found.state == "success"
