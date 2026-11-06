"""A batch is claimed under one lease and processed one document at a time. Each document gets
a full lease when its turn comes, fenced by the claim's generation, so slow sends early in a
batch never leave a later document too little lease to send."""

from datetime import timedelta

import pytest
from tests.fakes.fake_einvoice_provider import TRANSIENT

pytestmark = pytest.mark.integration

# About what the sandbox took to answer a create on a busy day: three of these used to fill the
# lease of a whole batch.
SLOW_POST = timedelta(seconds=18)
BEFORE_SEND = timedelta(seconds=3)


async def test_backlog_of_slow_posts_sends_every_document_once(world):
    backlog = [await world.settle() for _ in range(40)]
    for settled in backlog:
        world.provider.receive_delay(settled.reference, SLOW_POST)
        # Token and rate limiter between the renewal and the grant: a renewal shorter than a
        # full lease leaves too little of it to send.
        world.provider.delay_before_send(settled.reference, BEFORE_SEND)
    outcomes = []
    for _ in range(40):
        outcomes += [result.outcome for result in await world.issue()]
        world.advance(timedelta(seconds=1))
    assert "skipped:lease" not in outcomes and "lease_lost" not in outcomes
    assert outcomes.count("pending_provider") == len(backlog)
    assert world.metrics.count("invoice_post_skipped_total", reason="lease") == 0
    for settled in backlog:
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.post_count, doc.last_error_code) == ("pending_provider", 1, None)
        assert doc.attempts <= 1
        assert len(world.provider.posts(settled.reference)) == 1
    assert await world.cases(backlog[-1].document_id) == []


async def test_lease_renewal_is_fenced_by_the_claim_generation(world):
    # The second document already sent once without a clear answer, so a claim on it starts
    # with a lookup at the provider: a lost claim must not get that far.
    second = await world.settle()
    world.provider.script_create(second.reference, TRANSIENT)
    assert [result.outcome for result in await world.issue()] == ["backoff"]
    first = await world.settle()
    world.advance(timedelta(minutes=1))
    world.provider.receive_delay(first.reference, timedelta(seconds=30))
    before = await world.doc(second.document_id)
    calls_before = len(world.provider.calls)

    async def operator_change():
        await world.set_columns(second.document_id, lease_generation=before.lease_generation + 2)

    # Fires inside the first document's create, before the second document's turn.
    world.provider.on_next_prepare(operator_change)
    claimed_at = world.clock.now()
    results = await world.issue()
    assert [(result.document_id, result.outcome) for result in results] == [
        (first.document_id, "pending_provider"),
        (second.document_id, "lease_lost"),
    ]
    doc = await world.doc(second.document_id)
    # The lost claim wrote nothing: the lease stays the one the batch was claimed with, and the
    # turn it never had costs no attempt.
    assert (doc.lease_until, doc.updated_at) == (
        claimed_at + timedelta(seconds=world.config.lease_seconds),
        claimed_at,
    )
    assert (doc.status, doc.post_count, doc.attempts) == ("queued", 1, before.attempts)
    assert {call.reference_code for call in world.provider.calls[calls_before:]} == {
        first.reference
    }
    assert world.metrics.count("invoice_lease_lost_total", loop="issue") == 1


async def test_a_document_taken_by_another_worker_before_its_turn_is_charged_once(world):
    # Claimed in settlement order: each document is due from the moment it settled.
    batch = []
    for _ in range(3):
        batch.append(await world.settle())
        world.advance(timedelta(seconds=1))
    first, second, third = batch
    other = world.build()
    lease = timedelta(seconds=world.config.lease_seconds)
    # After the first POST the clock is 25 s past the claim: the second document's turn starts
    # there, while the third still holds the lease the batch was claimed with.
    world.provider.receive_delay(first.reference, timedelta(seconds=25))
    renewed_lease = []
    taken = []

    async def first_create() -> None:
        return None

    async def second_create() -> None:
        doc = await world.doc(second.document_id)
        renewed_lease.append(doc.lease_until - world.clock.now())
        # The batch's lease has run out: an idle worker takes what it can before this POST.
        world.advance(timedelta(seconds=36))
        assert other.issue_queued is not None
        taken.extend(await other.issue_queued.run_batch())

    # Each create takes the next hook, in batch order.
    world.provider.on_next_prepare(first_create)
    world.provider.on_next_prepare(second_create)
    results = await world.issue()
    # The second document got a full lease of its own when its turn started.
    assert renewed_lease == [lease]
    assert [(result.document_id, result.outcome) for result in taken] == [
        (third.document_id, "pending_provider")
    ]
    assert [(result.document_id, result.outcome) for result in results] == [
        (first.document_id, "pending_provider"),
        (second.document_id, "pending_provider"),
        (third.document_id, "lease_lost"),
    ]
    # The third document was claimed twice but processed once: one attempt.
    for settled in batch:
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.post_count, doc.attempts) == ("pending_provider", 1, 1)
        assert len(world.provider.posts(settled.reference)) == 1
    assert world.metrics.count("invoice_lease_lost_total", loop="issue") == 1
