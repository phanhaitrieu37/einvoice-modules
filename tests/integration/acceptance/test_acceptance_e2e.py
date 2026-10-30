"""Acceptance on PostgreSQL with the fake provider: signed webhook to issued invoice.

Every settlement is made by payment-module from a signed transfer and reaches the invoice
module only through the outbox; nothing here calls a network.
"""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from tests.fakes.fake_einvoice_provider import BLOCK, LOST_RESPONSE, rejected
from tests.integration.acceptance.conftest import TENANT, lines_for

from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.money import LineInput
from invoice_module.runtime import InvoiceWorker, WorkerIntervals

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.acceptance]

MANUAL = DeliveryMode.MANUAL
FAST = WorkerIntervals(issue_s=0.01, poll_s=0.01, sweep_s=0.5, artifacts_s=0.01)


async def test_settlement_to_issued_without_manual_step(world):
    merchant = await world.merchant("branch-a", "0071000000001")
    order = await world.order(merchant, amount=150_000)
    await world.pay(order)
    assert await world.dispatch() == 1
    await world.drive(rounds=4)

    doc = await world.original(order)
    assert (doc.status, doc.kind, doc.delivery_mode) == ("issued", "original", "auto")
    assert doc.invoice_number and doc.pdf_uri and doc.xml_uri
    assert doc.pdf_sha256 and doc.xml_sha256
    assert doc.total_vnd == doc.settled_amount_vnd == 150_000
    assert doc.approved_by is None
    assert await world.reasons(doc.id) == []
    assert world.provider.ops() == ["create", "check_create", "fetch_artifacts"]
    [post] = world.provider.posts(doc.reference_code)
    assert doc.reference_code == str(doc.settlement_id)
    assert post.payload["reference_code"] == doc.reference_code


async def test_redelivery_100_times_multi_worker_one_original(world):
    merchant = await world.merchant("branch-a", "0071000000001")
    order = await world.order(merchant)
    await world.pay(order)
    assert await world.dispatch() == 1
    [event] = world.delivered.events
    reference = str(await world.settlement_of(order))
    # The create stays in flight at the provider while the event is delivered again.
    world.provider.script_create(reference, BLOCK)

    stop = asyncio.Event()
    workers = [InvoiceWorker(world.build_invoice(), FAST) for _ in range(3)]
    running = [asyncio.create_task(worker.run(stop)) for worker in workers]

    async def redeliver(times: int) -> None:
        for _ in range(times):
            await world.publisher.publish(event)

    async def posted() -> bool:
        return len(world.provider.posts(reference)) >= 1

    async def created() -> bool:
        return world.provider.effective_creates[reference] == 1

    try:
        await world.until(posted)
        await asyncio.gather(*(redeliver(25) for _ in range(4)))
        await asyncio.sleep(0.3)  # three workers keep claiming while the create is in flight
        world.provider.release(reference, respond="accepted")
        await world.until(created)
        await asyncio.sleep(0.3)
    finally:
        if not world.provider.gate(reference).event.is_set():
            world.provider.release(reference, deliver=False)
        stop.set()
        await asyncio.gather(*running)

    assert len(world.delivered.events) == 101
    documents, receipts = world.inv.documents, world.inv.settlement_receipts
    count = sa.func.count()
    originals = await world.scalar(
        sa.select(count)
        .select_from(documents)
        .where(documents.c.settlement_id == reference, documents.c.kind == "original")
    )
    assert originals == 1
    assert await world.scalar(sa.select(count).select_from(documents)) == 1
    assert await world.scalar(sa.select(count).select_from(receipts)) == 1
    assert world.provider.effective_creates == {reference: 1}
    assert len(world.provider.posts(reference)) == 1
    assert (await world.original(order)).status == "pending_provider"


async def _assert_traceable(world, document, now):
    """The state table: every original document either progresses on its own or is in front
    of a person (an open case, or reasons a named actor cleared)."""
    status = document.status
    if status == "queued":
        assert document.queued_at is not None
    elif status == "pending_provider":
        assert document.pending_since is not None
    elif status == "needs_draft":
        assert document.settled_at > now - timedelta(hours=1), "a stale needs_draft"
    elif status in ("awaiting_approval", "approved", "issued"):
        if status == "issued":
            assert document.invoice_number
    elif status in ("blocked", "failed"):
        reasons = await world.reasons(document.id)
        assert reasons, f"{status} without a review case"
        open_reasons = [
            item for case, item in reasons if case.status == "open" and item["status"] == "open"
        ]
        if not open_reasons:
            for _, item in reasons:
                assert item["status"] == "cleared", item
                assert (item.get("cleared_by") or "").strip(), item
                if item["reason"] == "DUPLICATE_SETTLEMENT":
                    assert item["resolution"] == "waive", item
    else:
        raise AssertionError(f"unexpected status {status}")


async def test_every_settlement_has_receipt_and_traceable_state(world):
    with_issuer = await world.merchant("branch-a", "0071000000001")
    without_issuer = await world.merchant("branch-b", "0071000000002", issuer=False)
    orders = []
    # Seven ordinary orders; one of them the provider refuses.
    ordinary = [await world.order(with_issuer, amount=100 + n) for n in range(7)]
    orders += ordinary
    # Three settlements whose host never submitted a draft.
    orders += [await world.order(with_issuer, draft=False) for _ in range(3)]
    # Two drafts that disagree with the amount paid.
    orders += [
        await world.order(with_issuer, amount=200, draft_lines=lines_for(250)) for _ in range(2)
    ]
    # Two settlements of a merchant without an issuer account.
    orders += [await world.order(without_issuer) for _ in range(2)]
    # Two manual orders.
    orders += [await world.order(with_issuer, mode=MANUAL) for _ in range(2)]
    for order in orders:
        await world.pay(order)
    assert await world.dispatch() == len(orders)

    # An order paid twice, one payment after the other.
    first = await world.order(with_issuer, host_ref_id=f"order-seq-{uuid4()}")
    second = await world.order(with_issuer, draft=False, host_ref_id=first.host_ref_id)
    await world.pay(first)
    assert await world.dispatch() == 1
    await world.pay(second)
    assert await world.dispatch() == 1
    # An order paid twice, both payments delivered at the same time.
    left = await world.order(with_issuer, host_ref_id=f"order-par-{uuid4()}")
    right = await world.order(with_issuer, draft=False, host_ref_id=left.host_ref_id)
    await world.pay(left)
    await world.pay(right)
    assert sum(await asyncio.gather(world.dispatch(1), world.dispatch(1))) == 2
    orders += [first, second, left, right]
    assert len(orders) == 20

    refused_reference = str(await world.settlement_of(ordinary[-1]))
    world.provider.script_create(refused_reference, rejected("validation_error"))
    await world.drive(rounds=6)
    # The sequential duplicate is waived by an operator; the concurrent one stays open.
    held = await world.original(second)
    await world.invoice.resolve_review.execute(
        await world.open_case_id(held.id), "waive", "acceptance-operator", "refunded"
    )

    pm_settlements, documents = world.pm.settlements, world.inv.documents
    receipts = world.inv.settlement_receipts
    count = sa.func.count()
    settled = await world.scalar(sa.select(count).select_from(pm_settlements))
    assert settled == 20
    assert await world.scalar(sa.select(count).select_from(receipts)) == settled
    without_original = await world.scalar(
        sa.select(count)
        .select_from(pm_settlements)
        .where(
            ~sa.exists().where(
                documents.c.settlement_id == pm_settlements.c.id, documents.c.kind == "original"
            )
        )
    )
    assert without_original == 0
    unreceipted = await world.scalar(
        sa.select(count)
        .select_from(pm_settlements)
        .where(~sa.exists().where(receipts.c.settlement_id == pm_settlements.c.id))
    )
    assert unreceipted == 0

    now = world.clock.now()
    originals = await world.rows(sa.select(documents).where(documents.c.kind == "original"))
    assert len(originals) == 20
    for document in originals:
        await _assert_traceable(world, document, now)
    statuses = {document.status for document in originals}
    assert {"issued", "needs_draft", "blocked", "failed", "awaiting_approval"} <= statuses
    reasons = {
        item["reason"] for document in originals for _, item in await world.reasons(document.id)
    }
    assert {
        "AMOUNT_MISMATCH",
        "ISSUER_ACCOUNT_UNAVAILABLE",
        "DUPLICATE_SETTLEMENT",
        "PROVIDER_REJECTED",
    } <= reasons

    # Past the draft deadline every settlement still without a draft is in front of a person.
    latest = max(document.settled_at for document in originals)
    world.clock.set(latest + timedelta(minutes=61))
    await world.invoice.escalate_overdue.run(world.clock.now())
    waiting = await world.rows(sa.select(documents).where(documents.c.status == "needs_draft"))
    assert len(waiting) == 3
    for document in waiting:
        open_reasons = [
            item["reason"]
            for case, item in await world.reasons(document.id)
            if case.status == "open" and item["status"] == "open"
        ]
        assert open_reasons == ["DRAFT_MISSING"]


async def test_negative_scope_and_amount_rejected_before_provider_call(world):
    merchant = await world.merchant("branch-a", "0071000000001")
    other = await world.merchant("branch-b", "0071000000002")
    order = await world.order(merchant, amount=200, draft_lines=lines_for(250))
    await world.pay(order)
    await world.dispatch()
    await world.drive(rounds=3)
    doc = await world.original(order)
    assert doc.status == "blocked"
    assert [item["reason"] for _, item in await world.reasons(doc.id)] == ["AMOUNT_MISMATCH"]
    documents, receipts = world.inv.documents, world.inv.settlement_receipts
    [receipt] = await world.rows(sa.select(receipts).where(receipts.c.document_id == doc.id))

    async def refused(statement):
        with pytest.raises(IntegrityError) as caught:
            async with world.engine.begin() as conn:
                await conn.execute(statement)
        name = caught.value.orig.__cause__.constraint_name
        return name.removeprefix(world.inv.prefix)

    # An original whose total is not the settled amount.
    assert (
        await refused(
            documents.update()
            .where(documents.c.id == doc.id)
            .values(
                total_vnd=250, document={"buyer": {}}, buyer_source="host", delivery_mode="auto"
            )
        )
        == "documents_original_total_ck"
    )
    # A receipt for the settlement with another amount.
    wrong_amount = receipt._asdict() | {"event_id": uuid4(), "amount_vnd": 250}
    assert await refused(receipts.insert().values(**wrong_amount)) == (
        "settlement_receipts_document_fk"
    )
    # A receipt of another merchant's scope for this document.
    wrong_scope = receipt._asdict() | {"event_id": uuid4(), "merchant_id": other.id}
    assert await refused(receipts.insert().values(**wrong_scope)) == (
        "settlement_receipts_document_fk"
    )
    # The document moved to another merchant's issuer account scope.
    accounts = world.inv.issuer_accounts
    other_account = await world.scalar(
        sa.select(accounts.c.id).where(accounts.c.merchant_id == other.id)
    )
    assert (
        await refused(
            documents.update()
            .where(documents.c.id == doc.id)
            .values(issuer_account_id=other_account)
        )
        == "documents_issuer_fk"
    )
    await world.drive(rounds=3)
    assert world.provider.calls == []
    assert (await world.original(order)).status == "blocked"


async def test_original_total_equals_settled_amount_for_all_issued(world):
    merchant = await world.merchant("branch-a", "0071000000001")
    carts = [
        lines_for(100),
        lines_for(358_800, tax_rate=10),
        lines_for(99_999, tax_rate=8),
        (
            LineInput("Synthetic A", "each", Decimal("1.5"), 120_000, 10, item_code="SYN-A"),
            LineInput(
                "Synthetic B",
                "each",
                Decimal("2"),
                50_000,
                8,
                discount_vnd=10_000,
                item_code="SYN-B",
            ),
        ),
        (LineInput("Synthetic C", "month", Decimal("3"), 33_333, 5, item_code="SYN-C"),),
    ]
    orders = [await world.order(merchant, lines=lines) for lines in carts]
    for order in orders:
        await world.pay(order)
    assert await world.dispatch() == len(orders)
    await world.drive(rounds=4)

    documents = world.inv.documents
    issued = await world.scalar(
        sa.select(sa.func.count())
        .select_from(documents)
        .where(documents.c.kind == "original", documents.c.status == "issued")
    )
    assert issued == len(carts)
    mismatched = await world.scalar(
        sa.select(sa.func.count())
        .select_from(documents)
        .where(
            documents.c.kind == "original",
            documents.c.status == "issued",
            documents.c.total_vnd != documents.c.settled_amount_vnd,
        )
    )
    assert mismatched == 0
    for order in orders:
        doc = await world.original(order)
        assert doc.settled_amount_vnd == order.amount_vnd
        received = world.provider.requests_received[doc.reference_code]
        assert received["reference_code"] == doc.reference_code


async def test_manual_mode_issues_only_after_approval(world):
    merchant = await world.merchant("branch-a", "0071000000001")
    order = await world.order(merchant, mode=MANUAL)
    await world.pay(order)
    await world.dispatch()
    await world.drive(rounds=5)

    doc = await world.original(order)
    assert (doc.status, doc.delivery_mode) == ("awaiting_approval", "manual")
    assert doc.invoice_number is None
    assert world.provider.calls == []
    assert world.provider.invoices == {}

    await world.invoice.approve_draft_invoice.execute(doc.id, "acceptance-accountant")
    await world.drive(rounds=4)
    doc = await world.original(order)
    assert doc.status == "issued"
    assert doc.pdf_uri and doc.xml_uri and doc.invoice_number
    assert doc.approved_by == "acceptance-accountant"
    assert doc.approved_at is not None
    assert len(world.provider.posts(doc.reference_code)) == 1
    documents = world.inv.documents
    count = sa.func.count()
    unapproved = await world.scalar(
        sa.select(count)
        .select_from(documents)
        .where(
            documents.c.delivery_mode == "manual",
            documents.c.status == "issued",
            documents.c.approved_by.is_(None),
        )
    )
    assert unapproved == 0
    manual_issued = await world.scalar(
        sa.select(count)
        .select_from(documents)
        .where(documents.c.delivery_mode == "manual", documents.c.status == "issued")
    )
    assert manual_issued == 1


async def test_manual_create_lost_response_after_approval_recovers_with_exactly_one_post(world):
    merchant = await world.merchant("branch-a", "0071000000001")
    order = await world.order(merchant, mode=MANUAL)
    await world.pay(order)
    await world.dispatch()
    await world.drive(rounds=2)
    doc = await world.original(order)
    assert doc.status == "awaiting_approval"

    world.provider.script_create(doc.reference_code, LOST_RESPONSE)
    await world.invoice.approve_draft_invoice.execute(doc.id, "acceptance-accountant")
    await world.drive(rounds=6)

    doc = await world.original(order)
    assert doc.status == "issued"
    assert doc.pdf_uri and doc.xml_uri
    assert doc.issued_date == world.provider.invoices[doc.reference_code].issued_date
    assert doc.invoice_number == world.provider.invoices[doc.reference_code].invoice_number
    assert len(world.provider.posts(doc.reference_code)) == 1
    assert world.provider.effective_creates == {doc.reference_code: 1}
    # Looked up before the one create, and again after its response was lost.
    assert world.provider.ops() == [
        "find_by_reference",
        "create",
        "find_by_reference",
        "fetch_artifacts",
    ]


async def test_duplicate_settlement_held_until_operator_decision(world):
    merchant = await world.merchant("branch-a", "0071000000001")
    outcomes = {}
    for decision in ("waive", "retry"):
        first = await world.order(merchant, host_ref_id=f"order-{decision}-{uuid4()}")
        await world.pay(first)
        await world.dispatch()
        await world.drive(rounds=4)
        assert (await world.original(first)).status == "issued"

        # The host submits a draft for the second payment as for any other.
        second = await world.order(merchant, host_ref_id=first.host_ref_id)
        await world.pay(second)
        await world.dispatch()
        await world.drive(rounds=5)
        held = await world.original(second)
        assert held.status == "blocked"
        assert [item["reason"] for _, item in await world.reasons(held.id)] == [
            "DUPLICATE_SETTLEMENT"
        ]
        assert world.provider.posts(held.reference_code) == []
        held_calls = [c for c in world.provider.calls if c.reference_code == held.reference_code]
        assert held_calls == []

        case_id = await world.open_case_id(held.id)
        await world.invoice.resolve_review.execute(
            case_id, decision, "acceptance-operator", f"{decision} after the bank statement"
        )
        await world.drive(rounds=5)
        outcomes[decision] = (await world.original(second), held.reference_code)

    waived, waived_reference = outcomes["waive"]
    assert waived.status == "blocked"
    assert [call for call in world.provider.calls if call.reference_code == waived_reference] == []
    [(case, item)] = await world.reasons(waived.id)
    assert (case.status, item["status"], item["resolution"]) == ("resolved", "cleared", "waive")
    assert item["cleared_by"] == "acceptance-operator"

    retried, retried_reference = outcomes["retry"]
    assert retried.status == "issued"
    assert retried.total_vnd == retried.settled_amount_vnd
    assert len(world.provider.posts(retried_reference)) == 1
    assert retried.tenant_id == TENANT
