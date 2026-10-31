"""A frozen request whose lookup finds nothing: the job is checked by reference before any resend.

The sandbox lookup did not see a create it had just accepted (404 while the create answered
409), so a lookup ``None`` is not proof that the provider never received the POST.
"""

from datetime import date, timedelta

import pytest
from tests.fakes.fake_einvoice_provider import (
    AUTH_FAILED,
    CONFLICT,
    CRASH_AFTER_RECEIVE,
    LOST_RESPONSE,
    NO_JOB,
    PROVIDER_UNCONFIRMED,
    TRANSIENT,
    check_rejected,
)
from tests.fakes.recording import Crash

from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.domain.enums import DeliveryMode
from invoice_module.ports.provider import ProviderInvoice, ProviderProgress, ProviderTransient

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


async def test_job_is_checked_at_the_post_budget_limit(world):
    """The last automatic POST loses its answer while its job runs: the check still decides."""
    settled = await world.settle()
    world.provider.script_create(settled.reference, TRANSIENT, LOST_RESPONSE)
    world.provider.new_invoice(settled.reference, pending_polls=1)
    world.provider.lag_lookup(settled.reference)
    assert [result.outcome for result in await world.issue()] == ["backoff"]
    world.past_lease()
    # No job yet: the second (last) POST goes out and its answer is lost.
    assert [result.outcome for result in await world.issue()] == ["backoff"]
    world.past_lease()
    doc = await world.doc(settled.document_id)
    assert (doc.post_count, doc.post_limit) == (2, 2)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "pending_provider"
    assert world.provider.ops() == [
        "create",
        "find_by_reference",
        "check_create",
        "create",
        "find_by_reference",
        "check_create",
    ]
    assert (doc.status, doc.tracking_code, doc.post_count) == (
        "pending_provider",
        settled.reference,
        2,
    )
    assert await world.cases(settled.document_id) == []
    world.advance(timedelta(seconds=3))
    await world.poll()
    assert (await world.doc(settled.document_id)).status == "issued"
    assert len(world.provider.posts(settled.reference)) == 2
    assert world.provider.effective_creates[settled.reference] == 1


@pytest.mark.parametrize(
    "answer",
    [
        ProviderProgress(state="pending"),
        ProviderProgress(state="success"),
        ProviderProgress(state="failed"),
        ProviderProgress(state="failed", error_code=PROVIDER_UNCONFIRMED),
    ],
    ids=["pending", "success", "failed", "unconfirmed"],
)
async def test_conflict_with_a_blind_lookup_goes_to_the_poll_when_the_job_answers(world, answer):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CONFLICT)
    world.provider.script_check(settled.reference, answer)
    now = world.clock.now()
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "pending_provider"
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert (doc.status, doc.tracking_code, doc.post_count, doc.request_authorized_resend) == (
        "pending_provider",
        settled.reference,
        1,
        False,
    )
    assert doc.pending_since == now
    assert await world.cases(settled.document_id) == []


async def test_conflict_with_no_job_at_the_provider_opens_the_review(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CONFLICT)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert (result.outcome, doc.status, doc.last_error_code) == (
        "failed",
        "failed",
        "document_existed_unresolved",
    )
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    details = await world.reason_details(settled.document_id, "DOCUMENT_EXISTED_UNRESOLVED")
    assert details == {"provider_state": "none"}


@pytest.mark.parametrize(
    "answer",
    [ProviderTransient(retry_after=timedelta(minutes=7)), check_rejected("BAD_REQUEST")],
    ids=["transient", "refused"],
)
async def test_conflict_with_an_unclear_check_backs_off_and_spends_the_grant(world, answer):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CONFLICT)
    world.provider.script_check(settled.reference, answer)
    await world.set_columns(settled.document_id, request_authorized_resend=True)
    now = world.clock.now()
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "backoff"
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert (doc.status, doc.last_error_code, doc.request_authorized_resend) == (
        "queued",
        "transient_error",
        False,
    )
    expected_delay = timedelta(minutes=7) if isinstance(answer, Exception) else timedelta(seconds=2)
    assert doc.next_attempt_at == now + expected_delay
    assert await world.cases(settled.document_id) == []


async def test_conflict_with_a_check_auth_failure_opens_auth_review_and_spends_the_grant(world):
    settled = await world.settle()
    world.provider.script_create(settled.reference, CONFLICT)
    world.provider.script_check(settled.reference, AUTH_FAILED)
    await world.set_columns(settled.document_id, request_authorized_resend=True)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert (result.outcome, doc.status, doc.last_error_code, doc.request_authorized_resend) == (
        "failed",
        "failed",
        "auth_failed",
        False,
    )
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert await world.reason_details(settled.document_id, "AUTH_FAILED") == {"phase": "check"}


async def test_unconfirmed_job_is_never_resent_even_with_operator_permission(world):
    settled = await lost_create(world, TRANSIENT)
    unconfirmed = ProviderProgress(
        state="failed", error_code=PROVIDER_UNCONFIRMED, provider_message="Synthetic"
    )
    world.provider.script_check(settled.reference, unconfirmed, unconfirmed)
    await world.set_columns(settled.document_id, request_authorized_resend=True)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "pending_provider"
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert (doc.status, doc.request_authorized_resend) == ("pending_provider", False)
    world.advance(timedelta(seconds=3))
    await world.poll()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "provider_failed")
    details = await world.reason_details(settled.document_id, "PROVIDER_REJECTED")
    assert details["unconfirmed"] is True
    assert len(world.provider.posts()) == 1


@pytest.mark.parametrize("answer", [TRANSIENT, check_rejected("BAD_REQUEST")])
async def test_a_check_that_never_answers_ends_in_the_max_attempts_review(world, answer):
    world.config = InvoiceModuleConfig(issue_max_attempts=3)
    world.restart()
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(settled.reference, *[answer] * 10)
    outcomes = []
    for _ in range(10):
        doc = await world.doc(settled.document_id)
        if doc.status != "queued":
            break
        world.clock.set(max(doc.next_attempt_at, world.clock.now()))
        outcomes += [result.outcome for result in await world.issue()]
    doc = await world.doc(settled.document_id)
    # The claim that spends the last attempt opens the review itself.
    assert outcomes == ["backoff", "failed"]
    assert world.provider.ops().count("check_create") == 2
    assert (doc.status, doc.last_error_code, doc.attempts) == (
        "failed",
        "max_attempts_exceeded",
        3,
    )
    details = await world.reason_details(settled.document_id, "MAX_ATTEMPTS_EXCEEDED")
    assert details == {"attempts": 3, "issue_max_attempts": 3}
    assert len(world.provider.posts()) == 1


async def test_transient_check_honours_retry_after(world):
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(
        settled.reference, ProviderTransient(retry_after=timedelta(minutes=7))
    )
    now = world.clock.now()
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "backoff"
    assert doc.next_attempt_at == now + timedelta(minutes=7)
    assert len(world.provider.posts()) == 1


@pytest.mark.parametrize("state", ["success", "draft", "cancelled", "pending"])
async def test_unfailed_job_is_polled_even_with_operator_permission(world, state):
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(settled.reference, ProviderProgress(state=state))
    await world.set_columns(settled.document_id, request_authorized_resend=True)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "pending_provider"
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    assert (doc.status, doc.tracking_code, doc.request_authorized_resend) == (
        "pending_provider",
        settled.reference,
        False,
    )
    assert len(world.provider.posts()) == 1


async def test_failed_job_with_permission_at_the_budget_limit_is_labelled_failed(world):
    settled = await lost_create(world, TRANSIENT)
    world.provider.script_check(settled.reference, ProviderProgress(state="failed"))
    await world.set_columns(settled.document_id, post_count=2, request_authorized_resend=True)
    [result] = await world.issue()
    doc = await world.doc(settled.document_id)
    assert (result.outcome, doc.status, doc.last_error_code) == (
        "failed",
        "failed",
        "max_attempts_exceeded",
    )
    assert world.provider.ops() == ["create", "find_by_reference", "check_create"]
    details = await world.reason_details(settled.document_id, "MAX_ATTEMPTS_EXCEEDED")
    assert details == {
        "post_count": 2,
        "post_limit": 2,
        "lookup_state": "failed",
        "date_resend_used": False,
    }
