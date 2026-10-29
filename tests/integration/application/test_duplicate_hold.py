"""A second settlement for the same order never issues on its own.

Operator resolutions belong to a later use case; these tests record them with the repository's
`clear_reason`, which is the write that use case performs.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from tests.support.invoice_world import build_invoice, sample_lines, seed_issuer_account

from invoice_module.application.attach_draft import (
    attach_draft_to_documents,
    find_draft_for_document,
)
from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.domain.document import SettledPayment
from invoice_module.domain.enums import DeliveryMode, Environment
from invoice_module.domain.money import LineInput

pytestmark = pytest.mark.integration

DUPLICATE = "DUPLICATE_SETTLEMENT"


def settled(**over):
    return SettledPayment(
        **{
            "event_id": uuid4(),
            "tenant_id": "tenant-test",
            "merchant_id": uuid4(),
            "environment": Environment.TEST,
            "intent_id": uuid4(),
            "settlement_id": uuid4(),
            "transaction_id": uuid4(),
            "amount_vnd": 100,
            "settled_at": datetime.now(UTC),
            "origin": "auto",
            "host_ref_type": "order",
            "host_ref_id": f"order-{uuid4()}",
        }
        | over
    )


def next_settlement(first, **over):
    return SettledPayment(
        **(
            first.__dict__
            | {
                "event_id": uuid4(),
                "intent_id": uuid4(),
                "settlement_id": uuid4(),
                "transaction_id": uuid4(),
            }
            | over
        )
    )


class _Rows:
    def __init__(self, engine, tables):
        self.engine, self.tables = engine, tables

    async def insert(self, table, values):
        async with self.engine.begin() as conn:
            await conn.execute(table.insert().values(**values))


async def world(engine, tables, payment):
    await seed_issuer_account(_Rows(engine, tables), merchant_id=payment.merchant_id)
    return build_invoice(engine, tables)


def draft(payment, intent_id, amount=100, mode=DeliveryMode.AUTO):
    lines = (
        sample_lines()
        if amount == 100
        else (LineInput("Service", "each", Decimal("1"), amount, 0, item_code="TEST-ITEM-1"),)
    )
    return SubmitDraftCommand(
        payment.tenant_id,
        payment.merchant_id,
        payment.host_ref_type,
        payment.host_ref_id,
        intent_id,
        lines,
        amount,
        delivery_mode=mode,
    )


async def state(engine, tables, document_id):
    async with engine.connect() as conn:
        document = (
            await conn.execute(
                sa.select(tables.documents).where(tables.documents.c.id == document_id)
            )
        ).one()
        cases = (
            await conn.execute(
                sa.select(tables.review_cases)
                .where(tables.review_cases.c.document_id == document_id)
                .order_by(tables.review_cases.c.opened_at)
            )
        ).all()
    return document, cases


async def draft_state(engine, tables, draft_id):
    table = tables.invoice_drafts
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(table.c.status, table.c.consumed_by_document_id).where(
                    table.c.id == draft_id
                )
            )
        ).one()
    return tuple(row)


def reasons(cases):
    return [
        (item["reason"], item["status"], item.get("resolution"))
        for case in cases
        for item in case.details["reasons"]
    ]


def assert_held(document):
    assert (document.status, document.queued_at, document.next_attempt_at) == (
        "blocked",
        None,
        None,
    )


async def resolve(module, document_id, reason, resolution, note="checked at the portal"):
    async with module.uow_factory() as uow:
        case_id = await uow.review_cases.get_open_id(document_id)
        assert case_id is not None
        await uow.review_cases.clear_reason(
            case_id, reason, resolution, "operator-test", note, datetime.now(UTC)
        )
        await uow.commit()


async def attach_explicitly(module, payment):
    """Attach the way the overdue sweep and the review recovery do: one locked document."""
    async with module.uow_factory() as uow:
        await uow.lock_host_ref(payment.tenant_id, payment.host_ref_type, payment.host_ref_id)
        document = await uow.documents.get_original(payment.tenant_id, payment.settlement_id)
        found = await find_draft_for_document(uow, document)
        assert found is not None
        outcomes = await attach_draft_to_documents(
            uow, found, uow.issuer_accounts.get_active, datetime.now(UTC), documents=[document]
        )
        await uow.commit()
    return outcomes


async def held_second_settlement(engine, tables, *, first_draft=True, second_amount=None):
    """Record a first settlement and a second one for the same order; return both results."""
    first = settled()
    module = await world(engine, tables, first)
    if first_draft:
        await module.submit_draft.execute(draft(first, first.intent_id))
    one = await module.record_settled_payment.execute(first)
    second = next_settlement(first)
    if second_amount is not None:
        await module.submit_draft.execute(draft(first, second.intent_id, second_amount))
    two = await module.record_settled_payment.execute(second)
    return module, first, second, one, two


@pytest.mark.asyncio
async def test_late_draft_never_queues_document_with_open_duplicate(engine, inv_tables):
    # Without any draft at settlement time.
    module, first, second, _, two = await held_second_settlement(engine, inv_tables)
    document, _ = await state(engine, inv_tables, two.document_id)
    assert_held(document)
    result = await module.submit_draft.execute(draft(first, second.intent_id))
    assert result.attached == ()
    document, cases = await state(engine, inv_tables, two.document_id)
    assert_held(document)
    assert (DUPLICATE, "open", None) in reasons(cases)

    # With a draft that did not match the settled amount.
    module, first, second, _, two = await held_second_settlement(
        engine, inv_tables, second_amount=200
    )
    result = await module.submit_draft.execute(draft(first, second.intent_id, 100))
    assert [(item.document_id, item.status) for item in result.attached] == [
        (two.document_id, "blocked")
    ]
    document, cases = await state(engine, inv_tables, two.document_id)
    assert_held(document)
    assert document.document is not None and document.delivery_mode == "auto"
    assert reasons(cases) == [
        ("AMOUNT_MISMATCH", "cleared", "attach_draft"),
        (DUPLICATE, "open", None),
    ]
    assert await draft_state(engine, inv_tables, result.draft_id) == (
        "consumed",
        two.document_id,
    )


@pytest.mark.asyncio
async def test_first_settlement_unaffected_by_duplicate_hold(engine, inv_tables):
    module, first, _, one, two = await held_second_settlement(engine, inv_tables, first_draft=False)
    waiting, first_cases = await state(engine, inv_tables, one.document_id)
    assert (waiting.status, first_cases) == ("needs_draft", [])
    result = await module.submit_draft.execute(draft(first, None))
    assert [(item.document_id, item.status) for item in result.attached] == [
        (one.document_id, "queued")
    ]
    queued, first_cases = await state(engine, inv_tables, one.document_id)
    assert (queued.status, first_cases) == ("queued", [])
    assert queued.queued_at is not None and queued.next_attempt_at is not None
    held, cases = await state(engine, inv_tables, two.document_id)
    assert_held(held)
    assert reasons(cases) == [(DUPLICATE, "open", None)]


@pytest.mark.asyncio
async def test_waived_duplicate_with_open_mismatch_stays_blocked_after_late_draft(
    engine, inv_tables
):
    module, first, second, _, two = await held_second_settlement(
        engine, inv_tables, second_amount=200
    )
    await resolve(module, two.document_id, DUPLICATE, "waive", "refunded to the buyer")
    _, cases = await state(engine, inv_tables, two.document_id)
    assert cases[0].status == "open" and cases[0].reason == "AMOUNT_MISMATCH"
    result = await module.submit_draft.execute(draft(first, second.intent_id, 100))
    assert [(item.document_id, item.status) for item in result.attached] == [
        (two.document_id, "blocked")
    ]
    document, cases = await state(engine, inv_tables, two.document_id)
    assert_held(document)
    assert reasons(cases) == [
        ("AMOUNT_MISMATCH", "cleared", "attach_draft"),
        (DUPLICATE, "cleared", "waive"),
    ]


@pytest.mark.asyncio
async def test_waived_duplicate_stays_blocked_on_explicit_attach(engine, inv_tables):
    module, first, second, _, two = await held_second_settlement(engine, inv_tables)
    async with module.uow_factory() as uow:
        await uow.review_cases.open_once(first.tenant_id, two.document_id, "DRAFT_MISSING", {})
        await uow.commit()
    await resolve(module, two.document_id, DUPLICATE, "waive", "credited to the next order")
    submitted = await module.submit_draft.execute(draft(first, second.intent_id))
    outcomes = await attach_explicitly(module, second)
    assert [(item.document_id, item.status) for item in outcomes] == [(two.document_id, "blocked")]
    assert await draft_state(engine, inv_tables, submitted.draft_id) == (
        "consumed",
        two.document_id,
    )
    document, cases = await state(engine, inv_tables, two.document_id)
    assert_held(document)
    assert document.document is not None
    assert reasons(cases) == [
        (DUPLICATE, "cleared", "waive"),
        ("DRAFT_MISSING", "cleared", "attach_draft"),
    ]


@pytest.mark.asyncio
async def test_waived_duplicate_in_closed_case_stays_blocked(engine, inv_tables):
    module, first, second, _, two = await held_second_settlement(engine, inv_tables)
    await resolve(module, two.document_id, DUPLICATE, "waive", "refunded to the buyer")
    _, cases = await state(engine, inv_tables, two.document_id)
    assert [(case.status, case.resolution) for case in cases] == [("resolved", "waive")]
    await module.submit_draft.execute(draft(first, second.intent_id))
    outcomes = await attach_explicitly(module, second)
    assert [item.status for item in outcomes] == ["blocked"]
    document, _ = await state(engine, inv_tables, two.document_id)
    assert_held(document)


@pytest.mark.asyncio
async def test_acknowledged_duplicate_stays_blocked(engine, inv_tables):
    module, first, second, _, two = await held_second_settlement(engine, inv_tables)
    await resolve(module, two.document_id, DUPLICATE, "acknowledge")
    await module.submit_draft.execute(draft(first, second.intent_id))
    outcomes = await attach_explicitly(module, second)
    assert [item.status for item in outcomes] == ["blocked"]
    document, _ = await state(engine, inv_tables, two.document_id)
    assert_held(document)


@pytest.mark.asyncio
async def test_retried_duplicate_releases_hold(engine, inv_tables):
    module, first, second, _, two = await held_second_settlement(
        engine, inv_tables, second_amount=200
    )
    await resolve(module, two.document_id, DUPLICATE, "retry", "second order delivered")
    result = await module.submit_draft.execute(
        draft(first, second.intent_id, 100, DeliveryMode.MANUAL)
    )
    assert [(item.document_id, item.status) for item in result.attached] == [
        (two.document_id, "queued")
    ]
    document, cases = await state(engine, inv_tables, two.document_id)
    assert (document.status, document.delivery_mode) == ("queued", "manual")
    assert document.queued_at is not None and document.next_attempt_at is not None
    assert reasons(cases) == [
        ("AMOUNT_MISMATCH", "cleared", "attach_draft"),
        (DUPLICATE, "cleared", "retry"),
    ]


async def two_mismatched_settlements(engine, tables):
    """An order-wide draft with the wrong total blocks both settlements on AMOUNT_MISMATCH."""
    first = settled()
    module = await world(engine, tables, first)
    await module.submit_draft.execute(draft(first, None, 200))
    one = await module.record_settled_payment.execute(first)
    two = await module.record_settled_payment.execute(next_settlement(first))
    return module, first, one, two


@pytest.mark.asyncio
async def test_corrected_draft_holds_only_the_duplicate_of_two_mismatched_settlements(
    engine, inv_tables
):
    module, first, one, two = await two_mismatched_settlements(engine, inv_tables)
    _, first_cases = await state(engine, inv_tables, one.document_id)
    _, second_cases = await state(engine, inv_tables, two.document_id)
    assert reasons(first_cases) == [("AMOUNT_MISMATCH", "open", None)]
    assert reasons(second_cases) == [
        ("AMOUNT_MISMATCH", "open", None),
        (DUPLICATE, "open", None),
    ]
    result = await module.submit_draft.execute(draft(first, None))
    assert sorted((item.document_id, item.status) for item in result.attached) == sorted(
        [(one.document_id, "queued"), (two.document_id, "blocked")]
    )
    queued, _ = await state(engine, inv_tables, one.document_id)
    assert queued.status == "queued" and queued.queued_at is not None
    held, cases = await state(engine, inv_tables, two.document_id)
    assert_held(held)
    assert reasons(cases) == [
        ("AMOUNT_MISMATCH", "cleared", "attach_draft"),
        (DUPLICATE, "open", None),
    ]


@pytest.mark.asyncio
async def test_draft_attaches_to_oldest_waiting_document_first(engine, inv_tables):
    module, first, one, two = await two_mismatched_settlements(engine, inv_tables)
    three = await module.record_settled_payment.execute(next_settlement(first))
    documents = inv_tables.documents
    inserted = [one.document_id, two.document_id, three.document_id]
    status = dict(zip(inserted, ["queued", "blocked", "blocked"], strict=True))
    # Backdate a document that is neither stored first nor has the smallest id, so storage
    # order, id order and newest-first order each put a different document first.
    oldest = next(item for item in inserted[1:] if item != min(inserted))
    async with engine.begin() as conn:
        earliest = sa.select(sa.func.min(documents.c.created_at)).where(
            documents.c.id.in_(inserted)
        )
        await conn.execute(
            documents.update()
            .where(documents.c.id == oldest)
            .values(created_at=earliest.scalar_subquery() - timedelta(seconds=1))
        )
    expected = [oldest] + [item for item in inserted if item != oldest]
    result = await module.submit_draft.execute(draft(first, None))
    assert [(item.document_id, item.status) for item in result.attached] == [
        (item, status[item]) for item in expected
    ]
    assert await draft_state(engine, inv_tables, result.draft_id) == ("consumed", oldest)


@pytest.mark.asyncio
async def test_duplicate_decision_reads_all_cases(engine, inv_tables):
    module, first, _, one, two = await held_second_settlement(engine, inv_tables)
    tenant = first.tenant_id

    async def decision(document_id):
        async with module.uow_factory() as uow:
            return await uow.review_cases.duplicate_decision(document_id)

    async def reopen_duplicate(document_id):
        async with module.uow_factory() as uow:
            await uow.review_cases.open_once(tenant, document_id, DUPLICATE, {})
            await uow.commit()

    assert await decision(one.document_id) == "none"
    async with module.uow_factory() as uow:
        await uow.review_cases.open_once(tenant, one.document_id, "AMOUNT_MISMATCH", {})
        await uow.commit()
    assert await decision(one.document_id) == "none"

    assert await decision(two.document_id) == "open"
    await resolve(module, two.document_id, DUPLICATE, "retry")
    assert await decision(two.document_id) == "retried"
    await reopen_duplicate(two.document_id)
    assert await decision(two.document_id) == "open"
    await resolve(module, two.document_id, DUPLICATE, "waive")
    assert await decision(two.document_id) == "waived"
    await reopen_duplicate(two.document_id)
    assert await decision(two.document_id) == "open"
    await resolve(module, two.document_id, DUPLICATE, "retry")
    assert await decision(two.document_id) == "waived"
    _, cases = await state(engine, inv_tables, two.document_id)
    assert [case.status for case in cases] == ["resolved", "resolved", "resolved"]

    third = next_settlement(first)
    three = await module.record_settled_payment.execute(third)
    await resolve(module, three.document_id, DUPLICATE, "acknowledge")
    assert await decision(three.document_id) == "open"


class _HeldInsert:
    """Holds the first document insert so a second settlement can race against it."""

    def __init__(self, factory):
        self._factory = factory
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self._first = True

    def __call__(self):
        return _HeldInsertUow(self, self._factory())


class _HeldInsertUow:
    def __init__(self, owner, wrapped):
        self._owner, self._wrapped = owner, wrapped

    async def __aenter__(self):
        uow = await self._wrapped.__aenter__()
        documents, owner = uow.documents, self._owner

        class Documents:
            def __getattr__(self, name):
                return getattr(documents, name)

            async def insert(self, row):
                if owner._first:
                    owner._first = False
                    owner.entered.set()
                    await owner.release.wait()
                return await documents.insert(row)

        uow.documents = Documents()
        return uow

    async def __aexit__(self, *args):
        await self._wrapped.__aexit__(*args)


@pytest.mark.asyncio
async def test_concurrent_duplicate_settlements_with_draft_queue_only_first(engine, inv_tables):
    first = settled()
    module = await world(engine, inv_tables, first)
    submitted = await module.submit_draft.execute(draft(first, None))
    second = next_settlement(first)
    factory = _HeldInsert(module.uow_factory)
    module.record_settled_payment._uow_factory = factory
    one_task = asyncio.create_task(module.record_settled_payment.execute(first))
    await asyncio.wait_for(factory.entered.wait(), 5)
    two_task = asyncio.create_task(module.record_settled_payment.execute(second))
    await asyncio.sleep(0.05)
    assert not two_task.done()
    factory.release.set()
    one, two = await asyncio.gather(one_task, two_task)
    queued, first_cases = await state(engine, inv_tables, one.document_id)
    held, cases = await state(engine, inv_tables, two.document_id)
    assert (queued.status, queued.draft_id, first_cases) == ("queued", submitted.draft_id, [])
    assert_held(held)
    assert (held.document, held.draft_id) == (None, None)
    assert cases[0].details["reasons"][0]["details"] == {
        "other_settlement_ids": [str(first.settlement_id)],
        "draft_found": False,
    }
