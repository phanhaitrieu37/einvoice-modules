import ast
import asyncio
import random
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from tests.support.invoice_world import build_invoice, sample_lines, seed_issuer_account

from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.domain.document import SettledPayment
from invoice_module.domain.enums import Environment
from invoice_module.domain.money import LineInput

pytestmark = pytest.mark.integration


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
            "host_ref_id": "order-test",
        }
        | over
    )


async def add_account(engine, tables, merchant_id):
    return await seed_issuer_account(
        type(
            "Rows",
            (),
            {
                "insert": lambda self, table, values: _insert(engine, table, values),
                "tables": tables,
            },
        )(),
        merchant_id=merchant_id,
    )


async def _insert(engine, table, values):
    async with engine.begin() as conn:
        await conn.execute(table.insert().values(**values))
    return values


async def record(module, value):
    return await module.record_settled_payment.execute(value)


async def counts(engine, tables, settlement_id):
    async with engine.connect() as conn:
        documents = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(tables.documents)
            .where(tables.documents.c.settlement_id == settlement_id)
        )
        receipts = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(tables.settlement_receipts)
            .where(tables.settlement_receipts.c.settlement_id == settlement_id)
        )
        return documents, receipts


@pytest.mark.asyncio
async def test_same_event_three_times_one_document(engine, inv_tables):
    payment = settled()
    module = build_invoice(engine, inv_tables)
    results = [await record(module, payment) for _ in range(3)]
    assert results[0].created and all(not item.created for item in results[1:])
    assert len({item.document_id for item in results}) == 1
    assert await counts(engine, inv_tables, payment.settlement_id) == (1, 1)


@pytest.mark.asyncio
async def test_concurrent_same_event_one_document(engine, inv_tables):
    payment = settled()
    module = build_invoice(engine, inv_tables)
    gate = asyncio.Event()

    async def run():
        await gate.wait()
        return await record(module, payment)

    tasks = [asyncio.create_task(run()) for _ in range(4)]
    gate.set()
    results = await asyncio.gather(*tasks)
    assert len({item.document_id for item in results}) == 1
    assert sum(item.created for item in results) == 1
    assert await counts(engine, inv_tables, payment.settlement_id) == (1, 1)


@pytest.mark.asyncio
async def test_second_event_same_settlement_links_same_document(engine, inv_tables):
    first = settled()
    second = SettledPayment(**(first.__dict__ | {"event_id": uuid4()}))
    module = build_invoice(engine, inv_tables)
    one = await record(module, first)
    two = await record(module, second)
    assert two.document_id == one.document_id and not two.created
    assert await counts(engine, inv_tables, first.settlement_id) == (1, 2)


@pytest.mark.asyncio
async def test_mark_consumed_preserves_first_document(engine, inv_tables):
    first = settled()
    await add_account(engine, inv_tables, first.merchant_id)
    module = build_invoice(engine, inv_tables)
    draft = await module.submit_draft.execute(
        SubmitDraftCommand(
            first.tenant_id,
            first.merchant_id,
            first.host_ref_type,
            first.host_ref_id,
            first.intent_id,
            sample_lines(),
            first.amount_vnd,
        )
    )
    first_result = await record(module, first)
    second = SettledPayment(
        **(
            first.__dict__
            | {
                "event_id": uuid4(),
                "intent_id": uuid4(),
                "settlement_id": uuid4(),
                "transaction_id": uuid4(),
            }
        )
    )
    second_result = await record(module, second)
    async with module.uow_factory() as uow:
        changed = await uow.drafts.mark_consumed(draft.draft_id, second_result.document_id)
        await uow.commit()
    async with engine.connect() as conn:
        consumed_by = await conn.scalar(
            sa.select(inv_tables.invoice_drafts.c.consumed_by_document_id).where(
                inv_tables.invoice_drafts.c.id == draft.draft_id
            )
        )
    assert changed is False
    assert consumed_by == first_result.document_id


class _BarrierDocuments:
    def __init__(self, wrapped, barrier, lock, count, recovery_entered, recovery_release):
        self._wrapped, self._barrier, self._lock, self._count = wrapped, barrier, lock, count
        self._recovery_entered, self._recovery_release = recovery_entered, recovery_release
        self._calls = 0

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    async def get_original(self, tenant_id, settlement_id):
        self._calls += 1
        if self._calls == 1:
            row = await self._wrapped.get_original(tenant_id, settlement_id)
            async with self._lock:
                if not self._barrier.is_set():
                    self._count[0] += 1
                    if self._count[0] >= 2:
                        self._barrier.set()
            await self._barrier.wait()
            return row
        self._recovery_entered.set()
        await self._recovery_release.wait()
        return await self._wrapped.get_original(tenant_id, settlement_id)


class _BarrierUowFactory:
    def __init__(self, factory):
        self._factory = factory
        self._barrier, self._lock, self._count = asyncio.Event(), asyncio.Lock(), [0]
        self.recovery_entered, self.recovery_release = asyncio.Event(), asyncio.Event()

    def __call__(self):
        return _BarrierUow(
            self._factory(),
            self._barrier,
            self._lock,
            self._count,
            self.recovery_entered,
            self.recovery_release,
        )


class _BarrierUow:
    def __init__(self, wrapped, barrier, lock, count, recovery_entered, recovery_release):
        self._wrapped, self._barrier, self._lock, self._count = wrapped, barrier, lock, count
        self._recovery_entered, self._recovery_release = recovery_entered, recovery_release

    async def __aenter__(self):
        await self._wrapped.__aenter__()
        self._wrapped.documents = _BarrierDocuments(
            self._wrapped.documents,
            self._barrier,
            self._lock,
            self._count,
            self._recovery_entered,
            self._recovery_release,
        )
        return self._wrapped

    async def __aexit__(self, *args):
        await self._wrapped.__aexit__(*args)


class _HeldInsertDocuments:
    def __init__(self, wrapped, entered, release, lock, state):
        self._wrapped, self._entered, self._release = wrapped, entered, release
        self._lock, self._state = lock, state

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    async def insert(self, row):
        async with self._lock:
            first = not self._state[0]
            self._state[0] = True
        if first:
            self._entered.set()
            await self._release.wait()
        return await self._wrapped.insert(row)


class _HeldInsertFactory:
    def __init__(self, factory):
        self._factory = factory
        self.entered, self.release, self.lock, self.state = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Lock(),
            [False],
        )

    def __call__(self):
        return _HeldInsertUow(self._factory(), self.entered, self.release, self.lock, self.state)


class _HeldInsertUow:
    def __init__(self, wrapped, entered, release, lock, state):
        self._wrapped, self._entered, self._release = wrapped, entered, release
        self._lock, self._state = lock, state

    async def __aenter__(self):
        await self._wrapped.__aenter__()
        self._wrapped.documents = _HeldInsertDocuments(
            self._wrapped.documents, self._entered, self._release, self._lock, self._state
        )
        return self._wrapped

    async def __aexit__(self, *args):
        await self._wrapped.__aexit__(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_account", [False, True])
async def test_concurrent_distinct_events_same_settlement_different_host_ref_two_receipts_one_document(  # noqa: E501
    engine,
    inv_tables,
    with_account,
):
    first = settled()
    second = SettledPayment(
        **(first.__dict__ | {"event_id": uuid4(), "host_ref_id": "other-order"})
    )
    base = build_invoice(engine, inv_tables)
    if with_account:
        await add_account(engine, inv_tables, first.merchant_id)
    base.record_settled_payment._uow_factory = _BarrierUowFactory(base.uow_factory)
    tasks = [asyncio.create_task(record(base, first)), asyncio.create_task(record(base, second))]
    await asyncio.wait_for(base.record_settled_payment._uow_factory.recovery_entered.wait(), 5)
    keys = (
        f"{first.tenant_id}:{first.host_ref_type}:{first.host_ref_id}",
        f"{second.tenant_id}:{second.host_ref_type}:{second.host_ref_id}",
    )
    async with engine.begin() as conn:
        can_lock_during_recovery = [
            await conn.scalar(
                sa.text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": key}
            )
            for key in keys
        ]
    base.record_settled_payment._uow_factory.recovery_release.set()
    results = await asyncio.gather(*tasks)
    assert len({item.document_id for item in results}) == 1
    assert sum(item.created for item in results) == 1
    assert can_lock_during_recovery.count(False) == 1
    assert await counts(engine, inv_tables, first.settlement_id) == (1, 2)


@pytest.mark.asyncio
async def test_missing_draft_creates_needs_draft(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    result = await record(build_invoice(engine, inv_tables), payment)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.documents.c.status,
                    inv_tables.documents.c.reference_code,
                    inv_tables.documents.c.total_vnd,
                ).where(inv_tables.documents.c.id == result.document_id)
            )
        ).one()
    assert tuple(row) == ("needs_draft", str(payment.settlement_id), None)


@pytest.mark.asyncio
async def test_amount_mismatch_blocks_with_review_and_no_snapshot(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    module = build_invoice(engine, inv_tables)
    await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            payment.merchant_id,
            payment.host_ref_type,
            payment.host_ref_id,
            payment.intent_id,
            (LineInput("Service", "each", Decimal("1"), 200, 0),),
            200,
        )
    )
    result = await record(module, payment)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.documents.c.status,
                    inv_tables.documents.c.document,
                    inv_tables.review_cases.c.reason,
                )
                .join(
                    inv_tables.review_cases,
                    inv_tables.review_cases.c.document_id == inv_tables.documents.c.id,
                )
                .where(inv_tables.documents.c.id == result.document_id)
            )
        ).one()
    assert tuple(row) == ("blocked", None, "AMOUNT_MISMATCH")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft_kind,found", [("missing", False), ("matching", True), ("mismatching", True)]
)
async def test_missing_issuer_account_blocks_with_receipt_matrix(
    engine, inv_tables, draft_kind, found
):
    payment = settled()
    module = build_invoice(engine, inv_tables)
    if draft_kind != "missing":
        amount = 100 if draft_kind == "matching" else 200
        await module.submit_draft.execute(
            SubmitDraftCommand(
                "tenant-test",
                payment.merchant_id,
                payment.host_ref_type,
                payment.host_ref_id,
                payment.intent_id,
                (LineInput("Service", "each", Decimal("1"), amount, 0),),
                amount,
            )
        )
    result = await record(module, payment)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.documents.c.status,
                    inv_tables.documents.c.issuer_account_id,
                    inv_tables.settlement_receipts.c.document_id,
                    inv_tables.review_cases.c.reason,
                    inv_tables.review_cases.c.details,
                )
                .join(
                    inv_tables.settlement_receipts,
                    inv_tables.settlement_receipts.c.document_id == inv_tables.documents.c.id,
                )
                .join(
                    inv_tables.review_cases,
                    inv_tables.review_cases.c.document_id == inv_tables.documents.c.id,
                )
                .where(inv_tables.documents.c.id == result.document_id)
            )
        ).one()
    assert row.status == "blocked" and row.issuer_account_id is None
    assert row.document_id == result.document_id and row.reason == "ISSUER_ACCOUNT_UNAVAILABLE"
    assert row.details["reasons"][0]["details"]["draft_found"] is found
    if found:
        async with engine.connect() as conn:
            draft = (
                await conn.execute(
                    sa.select(
                        inv_tables.invoice_drafts.c.status,
                        inv_tables.invoice_drafts.c.consumed_by_document_id,
                    ).where(inv_tables.invoice_drafts.c.host_ref_id == payment.host_ref_id)
                )
            ).one()
        assert tuple(draft) == ("open", None)


@pytest.mark.asyncio
async def test_missing_account_duplicate_settlement_opens_both_reasons(engine, inv_tables):
    first = settled()
    second = SettledPayment(
        **(
            first.__dict__
            | {"event_id": uuid4(), "settlement_id": uuid4(), "transaction_id": uuid4()}
        )
    )
    module = build_invoice(engine, inv_tables)
    await record(module, first)
    duplicate = await record(module, second)
    async with engine.connect() as conn:
        case = (
            await conn.execute(
                sa.select(inv_tables.review_cases.c.details).where(
                    inv_tables.review_cases.c.document_id == duplicate.document_id
                )
            )
        ).scalar_one()
    assert {item["reason"] for item in case["reasons"] if item["status"] == "open"} == {
        "ISSUER_ACCOUNT_UNAVAILABLE",
        "DUPLICATE_SETTLEMENT",
    }


@pytest.mark.asyncio
async def test_open_once_does_not_duplicate_an_open_reason(engine, inv_tables):
    payment = settled()
    module = build_invoice(engine, inv_tables)
    result = await record(module, payment)
    async with module.uow_factory() as uow:
        for _ in range(2):
            await uow.review_cases.open_once(
                payment.tenant_id, result.document_id, "ISSUER_ACCOUNT_UNAVAILABLE", {}
            )
        reasons = await uow.review_cases.open_reasons(result.document_id)
        await uow.commit()
    assert reasons.count("ISSUER_ACCOUNT_UNAVAILABLE") == 1


@pytest.mark.asyncio
async def test_matches_draft_by_intent_before_host_ref(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    module = build_invoice(engine, inv_tables)
    other_intent = uuid4()
    await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            payment.merchant_id,
            payment.host_ref_type,
            payment.host_ref_id,
            other_intent,
            (LineInput("Wrong", "each", Decimal("1"), 200, 0),),
            200,
        )
    )
    specific = await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            payment.merchant_id,
            payment.host_ref_type,
            "other-host",
            payment.intent_id,
            (LineInput("Right", "each", Decimal("1"), 100, 0),),
            100,
        )
    )
    result = await record(module, payment)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(inv_tables.documents.c.status, inv_tables.documents.c.draft_id).where(
                    inv_tables.documents.c.id == result.document_id
                )
            )
        ).one()
    assert row.status == "queued" and row.draft_id == specific.draft_id


@pytest.mark.asyncio
async def test_host_ref_fallback_ignores_draft_bound_to_other_intent(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    module = build_invoice(engine, inv_tables)
    await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            payment.merchant_id,
            payment.host_ref_type,
            payment.host_ref_id,
            uuid4(),
            (LineInput("Wrong", "each", Decimal("1"), 100, 0),),
            100,
        )
    )
    result = await record(module, payment)
    async with engine.connect() as conn:
        status = await conn.scalar(
            sa.select(inv_tables.documents.c.status).where(
                inv_tables.documents.c.id == result.document_id
            )
        )
    assert status == "needs_draft"


@pytest.mark.asyncio
async def test_inactive_issuer_account_is_not_selected(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.issuer_accounts.update()
            .where(inv_tables.issuer_accounts.c.merchant_id == payment.merchant_id)
            .values(status="disabled")
        )
    result = await record(build_invoice(engine, inv_tables), payment)
    async with engine.connect() as conn:
        state = await conn.execute(
            sa.select(
                inv_tables.documents.c.status, inv_tables.documents.c.issuer_account_id
            ).where(inv_tables.documents.c.id == result.document_id)
        )
    assert tuple(state.one()) == ("blocked", None)


@pytest.mark.asyncio
async def test_fallback_buyer_recorded_as_fallback(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    module = build_invoice(engine, inv_tables)
    await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            payment.merchant_id,
            payment.host_ref_type,
            payment.host_ref_id,
            payment.intent_id,
            sample_lines(),
            100,
        )
    )
    result = await record(module, payment)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.documents.c.buyer_source, inv_tables.documents.c.document
                ).where(inv_tables.documents.c.id == result.document_id)
            )
        ).one()
    assert row.buyer_source == "fallback"
    assert row.document["buyer"]["name"] == "TEST-FALLBACK-NAME"


@pytest.mark.asyncio
async def test_reference_code_is_settlement_id_before_any_network(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    result = await record(build_invoice(engine, inv_tables), payment)
    async with engine.connect() as conn:
        reference = await conn.scalar(
            sa.select(inv_tables.documents.c.reference_code).where(
                inv_tables.documents.c.id == result.document_id
            )
        )
    assert reference == str(payment.settlement_id)


def test_record_module_has_no_network_dependency():
    path = Path(__file__).parents[3] / "src/invoice_module/application/record_settled_payment.py"
    tree = ast.parse(path.read_text())
    modules = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert not any(
        name and (name == "httpx" or name.startswith("invoice_module.adapters.sepay"))
        for name in modules
    )


@pytest.mark.asyncio
async def test_duplicate_settlement_same_order_issues_and_opens_review(engine, inv_tables):
    first = settled()
    await add_account(engine, inv_tables, first.merchant_id)
    module = build_invoice(engine, inv_tables)
    first_draft = await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            first.merchant_id,
            "order",
            first.host_ref_id,
            first.intent_id,
            sample_lines(),
            100,
        )
    )
    one = await record(module, first)
    second = SettledPayment(
        **(
            first.__dict__
            | {
                "event_id": uuid4(),
                "intent_id": uuid4(),
                "settlement_id": uuid4(),
                "transaction_id": uuid4(),
            }
        )
    )
    await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            first.merchant_id,
            "order",
            first.host_ref_id,
            second.intent_id,
            sample_lines(),
            100,
        )
    )
    two = await record(module, second)
    async with engine.connect() as conn:
        statuses = (
            await conn.scalars(
                sa.select(inv_tables.documents.c.status)
                .where(inv_tables.documents.c.host_ref_id == first.host_ref_id)
                .order_by(inv_tables.documents.c.created_at)
            )
        ).all()
        cases = (
            await conn.execute(
                sa.select(
                    inv_tables.review_cases.c.document_id, inv_tables.review_cases.c.reason
                ).where(
                    inv_tables.review_cases.c.document_id.in_([one.document_id, two.document_id])
                )
            )
        ).all()
    assert statuses == ["queued", "queued"]
    assert cases == [(two.document_id, "DUPLICATE_SETTLEMENT")]
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(inv_tables.documents.c.draft_id).where(
                    inv_tables.documents.c.id == one.document_id
                )
            )
            == first_draft.draft_id
        )


@pytest.mark.asyncio
async def test_duplicate_settlement_with_amount_mismatch_keeps_one_open_case(engine, inv_tables):
    first = settled()
    await add_account(engine, inv_tables, first.merchant_id)
    module = build_invoice(engine, inv_tables)
    await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            first.merchant_id,
            "order",
            first.host_ref_id,
            first.intent_id,
            sample_lines(),
            100,
        )
    )
    await record(module, first)
    second = SettledPayment(
        **(
            first.__dict__
            | {
                "event_id": uuid4(),
                "intent_id": uuid4(),
                "settlement_id": uuid4(),
                "transaction_id": uuid4(),
            }
        )
    )
    await module.submit_draft.execute(
        SubmitDraftCommand(
            "tenant-test",
            first.merchant_id,
            "order",
            first.host_ref_id,
            second.intent_id,
            (LineInput("Service", "each", Decimal("1"), 200, 0),),
            200,
        )
    )
    two = await record(module, second)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.review_cases.c.reason,
                    inv_tables.review_cases.c.status,
                    inv_tables.review_cases.c.details,
                ).where(inv_tables.review_cases.c.document_id == two.document_id)
            )
        ).one()
    assert (row.reason, row.status) == ("AMOUNT_MISMATCH", "open")
    assert [item["reason"] for item in row.details["reasons"] if item["status"] == "open"] == [
        "AMOUNT_MISMATCH",
        "DUPLICATE_SETTLEMENT",
    ]


@pytest.mark.asyncio
async def test_concurrent_duplicate_settlements_without_draft_two_originals_one_review(
    engine, inv_tables
):
    first = settled()
    await add_account(engine, inv_tables, first.merchant_id)
    second = SettledPayment(
        **(
            first.__dict__
            | {
                "event_id": uuid4(),
                "intent_id": uuid4(),
                "settlement_id": uuid4(),
                "transaction_id": uuid4(),
            }
        )
    )
    module = build_invoice(engine, inv_tables)
    factory = _HeldInsertFactory(module.uow_factory)
    module.record_settled_payment._uow_factory = factory
    one_task = asyncio.create_task(record(module, first))
    await asyncio.wait_for(factory.entered.wait(), 5)
    two_task = asyncio.create_task(record(module, second))
    await asyncio.sleep(0.05)
    key = f"{first.tenant_id}:{first.host_ref_type}:{first.host_ref_id}"
    async with engine.begin() as conn:
        can_lock = await conn.scalar(
            sa.text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": key}
        )
        blocked = await conn.scalar(
            sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        )
    factory.release.set()
    one, two = await asyncio.gather(one_task, two_task)
    async with engine.connect() as conn:
        docs = (
            await conn.execute(
                sa.select(inv_tables.documents.c.id, inv_tables.documents.c.status).where(
                    inv_tables.documents.c.host_ref_id == first.host_ref_id
                )
            )
        ).all()
        reasons = (
            await conn.scalars(
                sa.select(inv_tables.review_cases.c.reason).where(
                    inv_tables.review_cases.c.document_id.in_([one.document_id, two.document_id])
                )
            )
        ).all()
        receipts = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(inv_tables.settlement_receipts)
            .where(inv_tables.settlement_receipts.c.host_ref_id == first.host_ref_id)
        )
    assert can_lock is False
    assert blocked >= 1
    assert len(docs) == 2 and all(item.status == "needs_draft" for item in docs)
    assert "DUPLICATE_SETTLEMENT" in reasons and receipts == 2


@pytest.mark.asyncio
async def test_concurrent_submit_draft_and_settlement_no_lost_wakeup(engine, inv_tables):
    merchant = uuid4()
    await add_account(engine, inv_tables, merchant)
    module = build_invoice(engine, inv_tables)
    for index in range(20):
        payment = settled(merchant_id=merchant, host_ref_id=f"race-{index}")
        command = SubmitDraftCommand(
            "tenant-test",
            merchant,
            "order",
            payment.host_ref_id,
            payment.intent_id,
            sample_lines(),
            100,
        )
        gate = asyncio.Event()

        async def submit(gate=gate, command=command):
            await gate.wait()
            return await module.submit_draft.execute(command)

        async def settle_one(gate=gate, payment=payment):
            await gate.wait()
            return await record(module, payment)

        operations = [submit, settle_one]
        random.Random(index).shuffle(operations)
        tasks = [asyncio.create_task(operation()) for operation in operations]
        gate.set()
        await asyncio.gather(*tasks)
        async with engine.connect() as conn:
            status = await conn.scalar(
                sa.select(inv_tables.documents.c.status).where(
                    inv_tables.documents.c.settlement_id == payment.settlement_id
                )
            )
        assert status == "queued"


@pytest.mark.asyncio
async def test_submit_draft_blocks_behind_record_settled_payment(engine, inv_tables):
    payment = settled()
    await add_account(engine, inv_tables, payment.merchant_id)
    module = build_invoice(engine, inv_tables)
    factory = _HeldInsertFactory(module.uow_factory)
    module.record_settled_payment._uow_factory = factory
    module.submit_draft._uow_factory = factory
    settlement = asyncio.create_task(record(module, payment))
    await asyncio.wait_for(factory.entered.wait(), 5)
    draft = asyncio.create_task(
        module.submit_draft.execute(
            SubmitDraftCommand(
                payment.tenant_id,
                payment.merchant_id,
                payment.host_ref_type,
                payment.host_ref_id,
                payment.intent_id,
                sample_lines(),
                payment.amount_vnd,
            )
        )
    )
    await asyncio.sleep(0.05)
    async with engine.begin() as conn:
        blocked = await conn.scalar(
            sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        )
        can_acquire = await conn.scalar(
            sa.text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{payment.tenant_id}:{payment.host_ref_type}:{payment.host_ref_id}"},
        )
    factory.release.set()
    await asyncio.gather(settlement, draft)
    assert blocked >= 1 and can_acquire is False
