from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.application.create_intent import CreateIntentCommand
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from tests.support.invoice_world import build_invoice, seed_issuer_account
from tests.support.payment_world import build_payment, onboard_payment, pm_tables

from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWork
from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.errors import DraftAlreadyConsumed, DraftBoundToOtherIntent

pytestmark = pytest.mark.integration


class _PendingBase(DeclarativeBase):
    pass


class _PendingRow(_PendingBase):
    __tablename__ = "pending_unit_of_work_state"
    id: Mapped[int] = mapped_column(primary_key=True)


def line_dict(name="Service", total=100):
    return {
        "name": name,
        "unit": "each",
        "quantity": Decimal("1"),
        "unit_price_vnd": total,
        "tax_rate": 0,
        "item_code": "TEST-ITEM-1",
    }


def draft_cmd(merchant_id, host_ref_id, intent_id, **over):
    over.setdefault("delivery_mode", DeliveryMode.AUTO)
    return SubmitDraftCommand(
        "tenant-test", merchant_id, "order", host_ref_id, intent_id, (line_dict(),), 100, **over
    )


class FixedClock:
    def __init__(self, current):
        self.current = current

    def now(self):
        return self.current


async def seed_account(engine, tables, merchant_id, tenant_id="tenant-test"):
    row = await seed_issuer_account(
        type("Rows", (), {"insert": lambda self, t, v: _insert(engine, t, v), "tables": tables})(),
        tenant_id=tenant_id,
        merchant_id=merchant_id,
    )
    return row


async def _insert(engine, table, values):
    async with engine.begin() as conn:
        await conn.execute(table.insert().values(**values))
    return values


async def seed_waiting_document(
    engine,
    tables,
    merchant_id,
    host_ref_id,
    intent_id=None,
    total=100,
    tenant_id="tenant-test",
    host_ref_type="order",
):
    async with engine.connect() as conn:
        account_id = await conn.scalar(
            sa.select(tables.issuer_accounts.c.id).where(
                tables.issuer_accounts.c.tenant_id == tenant_id,
                tables.issuer_accounts.c.merchant_id == merchant_id,
            )
        )
    values = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "merchant_id": merchant_id,
        "environment": "test",
        "issuer_account_id": account_id,
        "kind": "original",
        "settlement_id": uuid4(),
        "intent_id": intent_id or uuid4(),
        "host_ref_type": host_ref_type,
        "host_ref_id": host_ref_id,
        "reference_code": str(uuid4()),
        "settled_amount_vnd": total,
        "settled_at": datetime.now(UTC),
        "status": "needs_draft",
    }
    async with engine.begin() as conn:
        await conn.execute(tables.documents.insert().values(**values))
    return values


async def create_payment_context(engine, publisher=None):
    tables = pm_tables(f"pm_{uuid4().hex[:8]}_")
    async with engine.begin() as conn:
        await conn.run_sync(tables.merchants.metadata.create_all)
    module = build_payment(engine, tables, publisher)
    merchant, receiving, connection = await onboard_payment(module)
    return tables, module, merchant, receiving, connection


@pytest.mark.asyncio
async def test_submit_draft_joined_commits_with_create_intent(engine, inv_tables):
    pm, payment, merchant, account, _ = await create_payment_context(engine)
    invoice = build_invoice(engine, inv_tables)
    host_ref = f"joined-{uuid4()}"
    cmd = CreateIntentCommand(
        "tenant-test",
        amount_vnd=100,
        host_ref_type="order",
        host_ref_id=host_ref,
        idempotency_key=host_ref,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        prefix_name="bill",
        merchant_id=merchant.id,
        receiving_account_id=account.id,
    )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
        created = await payment.create_intent.execute(
            cmd, uow=SqlAlchemyUnitOfWork.joined(session, pm)
        )
        result = await invoice.submit_draft.execute(
            draft_cmd(merchant.id, host_ref, created.intent.id),
            uow=SqlAlchemyInvoiceUnitOfWork.joined(session, inv_tables),
        )
    assert result.revision == 1
    async with engine.connect() as conn:
        assert await conn.scalar(
            sa.select(pm.payment_intents.c.id).where(pm.payment_intents.c.id == created.intent.id)
        )
        assert await conn.scalar(
            sa.select(inv_tables.invoice_drafts.c.id).where(
                inv_tables.invoice_drafts.c.id == result.draft_id
            )
        )


@pytest.mark.asyncio
async def test_submit_draft_joined_rolls_back_with_create_intent(engine, inv_tables):
    pm, payment, merchant, account, _ = await create_payment_context(engine)
    invoice = build_invoice(engine, inv_tables)
    host_ref = f"rollback-{uuid4()}"
    cmd = CreateIntentCommand(
        "tenant-test",
        amount_vnd=100,
        host_ref_type="order",
        host_ref_id=host_ref,
        idempotency_key=host_ref,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        prefix_name="bill",
        merchant_id=merchant.id,
        receiving_account_id=account.id,
    )
    intent_id = None
    with pytest.raises(RuntimeError, match="host rollback"):
        async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
            created = await payment.create_intent.execute(
                cmd, SqlAlchemyUnitOfWork.joined(session, pm)
            )
            intent_id = created.intent.id
            await invoice.submit_draft.execute(
                draft_cmd(merchant.id, host_ref, intent_id),
                SqlAlchemyInvoiceUnitOfWork.joined(session, inv_tables),
            )
            raise RuntimeError("host rollback")
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(pm.payment_intents.c.id).where(pm.payment_intents.c.id == intent_id)
            )
            is None
        )
        assert (
            await conn.scalar(
                sa.select(inv_tables.invoice_drafts.c.id).where(
                    inv_tables.invoice_drafts.c.host_ref_id == host_ref
                )
            )
            is None
        )


@pytest.mark.asyncio
async def test_resubmit_same_intent_increments_revision(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    cmd = draft_cmd(merchant, "revision", intent)
    first = await module.submit_draft.execute(cmd)
    second = await module.submit_draft.execute(cmd)
    assert (first.revision, second.revision) == (1, 2)


@pytest.mark.asyncio
async def test_draft_update_timestamp_uses_injected_clock(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    module.submit_draft._clock = clock
    merchant, intent = uuid4(), uuid4()
    first = await module.submit_draft.execute(draft_cmd(merchant, "clocked", intent))
    expected = datetime(2026, 1, 2, tzinfo=UTC)
    clock.current = expected
    await module.submit_draft.execute(draft_cmd(merchant, "clocked", intent))
    async with engine.connect() as conn:
        updated_at = await conn.scalar(
            sa.select(inv_tables.invoice_drafts.c.updated_at).where(
                inv_tables.invoice_drafts.c.id == first.draft_id
            )
        )
    assert updated_at == expected


@pytest.mark.asyncio
async def test_rebind_open_draft_to_new_intent_when_old_unsettled(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    first = await module.submit_draft.execute(draft_cmd(merchant, "rebind-open", uuid4()))
    new_intent = uuid4()
    second = await module.submit_draft.execute(draft_cmd(merchant, "rebind-open", new_intent))
    async with engine.connect() as conn:
        bound_intent = await conn.scalar(
            sa.select(inv_tables.invoice_drafts.c.intent_id).where(
                inv_tables.invoice_drafts.c.id == first.draft_id
            )
        )
    assert second.draft_id == first.draft_id and second.revision == 2
    assert bound_intent == new_intent


@pytest.mark.asyncio
async def test_open_draft_cannot_be_unbound_with_null_intent(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    await module.submit_draft.execute(draft_cmd(merchant, "no-unbind", intent))
    with pytest.raises(DraftBoundToOtherIntent):
        await module.submit_draft.execute(draft_cmd(merchant, "no-unbind", None))


@pytest.mark.asyncio
async def test_rebind_refused_when_old_intent_settled(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, old_intent = uuid4(), uuid4()
    await module.submit_draft.execute(draft_cmd(merchant, "bound", old_intent))
    await seed_account(engine, inv_tables, merchant)
    await seed_waiting_document(engine, inv_tables, merchant, "bound", old_intent)
    with pytest.raises(DraftBoundToOtherIntent):
        await module.submit_draft.execute(draft_cmd(merchant, "bound", uuid4()))


@pytest.mark.asyncio
async def test_second_checkout_reopens_consumed_draft_with_new_intent(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, old_intent = uuid4(), uuid4()
    first = await module.submit_draft.execute(draft_cmd(merchant, "second", old_intent))
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "second", old_intent)
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.invoice_drafts.update()
            .where(inv_tables.invoice_drafts.c.id == first.draft_id)
            .values(status="consumed", consumed_by_document_id=doc["id"])
        )
    new_intent = uuid4()
    second = await module.submit_draft.execute(draft_cmd(merchant, "second", new_intent))
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.invoice_drafts.c.intent_id,
                    inv_tables.invoice_drafts.c.status,
                ).where(inv_tables.invoice_drafts.c.id == first.draft_id)
            )
        ).one()
    assert second.draft_id == first.draft_id and second.revision == 2
    assert row.status == "open" and row.intent_id == new_intent


@pytest.mark.asyncio
async def test_consumed_draft_rejects_update_for_same_intent(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    first = await module.submit_draft.execute(draft_cmd(merchant, "consumed", intent))
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "consumed", intent)
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.invoice_drafts.update()
            .where(inv_tables.invoice_drafts.c.id == first.draft_id)
            .values(status="consumed", consumed_by_document_id=doc["id"])
        )
    with pytest.raises(DraftAlreadyConsumed):
        await module.submit_draft.execute(draft_cmd(merchant, "consumed", intent))


@pytest.mark.asyncio
async def test_discarded_draft_reopens(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    first = await module.submit_draft.execute(draft_cmd(merchant, "discarded", intent))
    await module.discard_draft.execute("tenant-test", "order", "discarded", "test-operator")
    reopened = await module.submit_draft.execute(draft_cmd(merchant, "discarded", intent))
    assert reopened.draft_id == first.draft_id and reopened.revision == 3


@pytest.mark.asyncio
async def test_discarded_bound_draft_cannot_be_reopened_without_intent(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    await module.submit_draft.execute(draft_cmd(merchant, "discarded-no-unbind", intent))
    await module.discard_draft.execute(
        "tenant-test", "order", "discarded-no-unbind", "test-operator"
    )
    with pytest.raises(DraftBoundToOtherIntent):
        await module.submit_draft.execute(draft_cmd(merchant, "discarded-no-unbind", None))


@pytest.mark.asyncio
async def test_consumed_bound_draft_cannot_be_unbound_with_null_intent(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "consumed-no-unbind", intent)
    first = await module.submit_draft.execute(draft_cmd(merchant, "consumed-no-unbind", intent))
    assert [(item.document_id, item.status) for item in first.attached] == [(doc["id"], "queued")]
    with pytest.raises(DraftBoundToOtherIntent):
        await module.submit_draft.execute(draft_cmd(merchant, "consumed-no-unbind", None))
    async with engine.connect() as conn:
        draft = (
            await conn.execute(
                sa.select(
                    inv_tables.invoice_drafts.c.status,
                    inv_tables.invoice_drafts.c.intent_id,
                    inv_tables.invoice_drafts.c.revision,
                    inv_tables.invoice_drafts.c.consumed_by_document_id,
                ).where(inv_tables.invoice_drafts.c.id == first.draft_id)
            )
        ).one()
    assert tuple(draft) == ("consumed", intent, 1, doc["id"])


@pytest.mark.asyncio
async def test_unbound_draft_ignores_waiting_documents_outside_its_scope(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    host_ref = f"scope-{uuid4()}"
    await seed_account(engine, inv_tables, merchant)
    await seed_account(engine, inv_tables, merchant, tenant_id="tenant-other")
    outside = [
        await seed_waiting_document(engine, inv_tables, merchant, f"other-{uuid4()}"),
        await seed_waiting_document(
            engine, inv_tables, merchant, host_ref, host_ref_type="booking"
        ),
        await seed_waiting_document(
            engine, inv_tables, merchant, host_ref, tenant_id="tenant-other"
        ),
    ]
    ids = [doc["id"] for doc in outside]

    async def snapshot():
        async with engine.connect() as conn:
            documents = (
                await conn.execute(
                    sa.select(inv_tables.documents)
                    .where(inv_tables.documents.c.id.in_(ids))
                    .order_by(inv_tables.documents.c.id)
                )
            ).all()
            cases = await conn.scalar(
                sa.select(sa.func.count())
                .select_from(inv_tables.review_cases)
                .where(inv_tables.review_cases.c.document_id.in_(ids))
            )
        return documents, cases

    before = await snapshot()
    assert [row.status for row in before[0]] == ["needs_draft"] * 3 and before[1] == 0
    result = await module.submit_draft.execute(draft_cmd(merchant, host_ref, None))
    assert result.attached == ()
    assert await snapshot() == before


@pytest.mark.asyncio
async def test_late_draft_moves_needs_draft_to_queued_and_clears_review(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "late")
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.review_cases.insert().values(
                id=uuid4(),
                tenant_id="tenant-test",
                document_id=doc["id"],
                reason="DRAFT_MISSING",
                details={"reasons": [{"reason": "DRAFT_MISSING", "details": {}, "status": "open"}]},
                status="open",
            )
        )
    result = await module.submit_draft.execute(draft_cmd(merchant, "late", doc["intent_id"]))
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(inv_tables.documents.c.status).where(
                    inv_tables.documents.c.id == doc["id"]
                )
            )
            == "queued"
        )
        assert (
            await conn.scalar(
                sa.select(
                    inv_tables.review_cases.c.status, inv_tables.review_cases.c.details
                ).where(inv_tables.review_cases.c.document_id == doc["id"])
            )
            == "resolved"
        )
        reasons = (
            await conn.execute(
                sa.select(inv_tables.review_cases.c.details).where(
                    inv_tables.review_cases.c.document_id == doc["id"]
                )
            )
        ).scalar_one()["reasons"]
        assert all(reason["status"] == "cleared" for reason in reasons)
    assert result.attached[0].status == "queued"


@pytest.mark.asyncio
async def test_late_draft_without_active_account_opens_issuer_review(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "late-no-account")
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.issuer_accounts.update()
            .where(inv_tables.issuer_accounts.c.merchant_id == merchant)
            .values(status="disabled")
        )
    await module.submit_draft.execute(draft_cmd(merchant, "late-no-account", doc["intent_id"]))
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.documents.c.status,
                    inv_tables.review_cases.c.reason,
                    inv_tables.review_cases.c.details,
                )
                .join(
                    inv_tables.review_cases,
                    inv_tables.review_cases.c.document_id == inv_tables.documents.c.id,
                )
                .where(inv_tables.documents.c.id == doc["id"])
            )
        ).one()
    assert row.status == "needs_draft"
    assert row.reason == "ISSUER_ACCOUNT_UNAVAILABLE"
    assert row.details["reasons"][0]["details"]["draft_found"] is True


@pytest.mark.asyncio
async def test_late_draft_clears_issuer_unavailable_after_account_activation(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    account = await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "activate-after-missing")
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.issuer_accounts.update()
            .where(inv_tables.issuer_accounts.c.id == account["id"])
            .values(status="disabled")
        )
    first = await module.submit_draft.execute(
        draft_cmd(merchant, "activate-after-missing", doc["intent_id"])
    )
    assert first.attached == ()
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.issuer_accounts.update()
            .where(inv_tables.issuer_accounts.c.id == account["id"])
            .values(status="active")
        )
    second = await module.submit_draft.execute(
        draft_cmd(merchant, "activate-after-missing", doc["intent_id"])
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.documents.c.status,
                    inv_tables.review_cases.c.status,
                    inv_tables.review_cases.c.details,
                )
                .join(
                    inv_tables.review_cases,
                    inv_tables.review_cases.c.document_id == inv_tables.documents.c.id,
                )
                .where(inv_tables.documents.c.id == doc["id"])
            )
        ).one()
    assert second.attached[0].status == "queued"
    assert tuple(row[:2]) == ("queued", "resolved")
    assert all(item["status"] == "cleared" for item in row.details["reasons"])


@pytest.mark.asyncio
async def test_late_draft_attaches_all_waiting_documents(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    await seed_account(engine, inv_tables, merchant)
    first = await seed_waiting_document(engine, inv_tables, merchant, "two-waiting")
    second = await seed_waiting_document(engine, inv_tables, merchant, "two-waiting")
    result = await module.submit_draft.execute(draft_cmd(merchant, "two-waiting", None))
    async with engine.connect() as conn:
        statuses = (
            await conn.scalars(
                sa.select(inv_tables.documents.c.status).where(
                    inv_tables.documents.c.id.in_([first["id"], second["id"]])
                )
            )
        ).all()
        consumed = await conn.scalar(
            sa.select(inv_tables.invoice_drafts.c.consumed_by_document_id).where(
                inv_tables.invoice_drafts.c.id == result.draft_id
            )
        )
    assert statuses == ["queued", "queued"]
    assert consumed in {first["id"], second["id"]}


@pytest.mark.asyncio
async def test_late_draft_skips_document_of_other_intent(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "other-intent", uuid4())
    result = await module.submit_draft.execute(draft_cmd(merchant, "other-intent", uuid4()))
    async with engine.connect() as conn:
        status = await conn.scalar(
            sa.select(inv_tables.documents.c.status).where(inv_tables.documents.c.id == doc["id"])
        )
    assert status == "needs_draft" and result.attached == ()


@pytest.mark.asyncio
async def test_late_draft_amount_mismatch_blocks_and_switches_reason(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(engine, inv_tables, merchant, "mismatch", total=200)
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.review_cases.insert().values(
                id=uuid4(),
                tenant_id="tenant-test",
                document_id=doc["id"],
                reason="DRAFT_MISSING",
                details={"reasons": [{"reason": "DRAFT_MISSING", "details": {}, "status": "open"}]},
                status="open",
            )
        )
    await module.submit_draft.execute(draft_cmd(merchant, "mismatch", doc["intent_id"]))
    async with engine.connect() as conn:
        statement = (
            sa.select(
                inv_tables.documents.c.status,
                inv_tables.documents.c.queued_at,
                inv_tables.documents.c.next_attempt_at,
                inv_tables.review_cases.c.reason,
                inv_tables.review_cases.c.status,
                inv_tables.review_cases.c.details,
            )
            .join(
                inv_tables.review_cases,
                inv_tables.review_cases.c.document_id == inv_tables.documents.c.id,
            )
            .where(inv_tables.documents.c.id == doc["id"])
        )
        row = (await conn.execute(statement)).one()
    assert tuple(row[:5]) == ("blocked", None, None, "AMOUNT_MISMATCH", "open")
    assert row.details["reasons"][0]["status"] == "cleared"
    assert row.details["reasons"][1]["reason"] == "AMOUNT_MISMATCH"


@pytest.mark.asyncio
async def test_corrected_draft_clears_mismatch_but_duplicate_stays_blocked(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    await seed_account(engine, inv_tables, merchant)
    doc = await seed_waiting_document(
        engine, inv_tables, merchant, "duplicate-correction", total=100
    )
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.documents.update()
            .where(inv_tables.documents.c.id == doc["id"])
            .values(status="blocked")
        )
        await conn.execute(
            inv_tables.review_cases.insert().values(
                id=uuid4(),
                tenant_id="tenant-test",
                document_id=doc["id"],
                reason="DUPLICATE_SETTLEMENT",
                details={
                    "reasons": [
                        {"reason": "DUPLICATE_SETTLEMENT", "details": {}, "status": "open"},
                        {"reason": "AMOUNT_MISMATCH", "details": {}, "status": "open"},
                    ]
                },
                status="open",
            )
        )
    result = await module.submit_draft.execute(
        draft_cmd(
            merchant, "duplicate-correction", doc["intent_id"], delivery_mode=DeliveryMode.MANUAL
        )
    )
    async with engine.connect() as conn:
        state = (
            await conn.execute(
                sa.select(
                    inv_tables.documents, inv_tables.review_cases.c.reason.label("case_reason")
                )
                .add_columns(inv_tables.review_cases.c.status.label("case_status"))
                .add_columns(inv_tables.review_cases.c.details.label("case_details"))
                .join(
                    inv_tables.review_cases,
                    inv_tables.review_cases.c.document_id == inv_tables.documents.c.id,
                )
                .where(inv_tables.documents.c.id == doc["id"])
            )
        ).one()
    assert [(item.document_id, item.status) for item in result.attached] == [(doc["id"], "blocked")]
    assert (state.status, state.queued_at, state.next_attempt_at) == ("blocked", None, None)
    assert (state.draft_id, state.delivery_mode, state.total_vnd) == (
        result.draft_id,
        "manual",
        100,
    )
    assert state.document is not None
    assert (state.case_reason, state.case_status) == ("DUPLICATE_SETTLEMENT", "open")
    assert [(item["reason"], item["status"]) for item in state.case_details["reasons"]] == [
        ("DUPLICATE_SETTLEMENT", "open"),
        ("AMOUNT_MISMATCH", "cleared"),
    ]


@pytest.mark.asyncio
async def test_discard_rejects_non_open_draft(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    draft = await module.submit_draft.execute(draft_cmd(merchant, "discard-consumed", intent))
    async with engine.begin() as conn:
        await conn.execute(
            inv_tables.invoice_drafts.update()
            .where(inv_tables.invoice_drafts.c.id == draft.draft_id)
            .values(status="discarded")
        )
    from invoice_module.domain.errors import DraftNotOpen

    with pytest.raises(DraftNotOpen):
        await module.discard_draft.execute("tenant-test", "order", "discard-consumed", "actor")


@pytest.mark.asyncio
async def test_joined_lock_rejects_pending_orm_state_before_lock(engine, inv_tables):
    async with (
        async_sessionmaker(engine, expire_on_commit=False)() as session,
        SqlAlchemyInvoiceUnitOfWork.joined(session, inv_tables) as uow,
    ):
        session.add(_PendingRow(id=1))
        with pytest.raises(RuntimeError, match="lock_host_ref must precede joined work"):
            await uow.lock_host_ref("tenant-test", "order", "pending")


async def draft_delivery_mode(engine, tables, draft_id):
    async with engine.connect() as conn:
        return await conn.scalar(
            sa.select(tables.invoice_drafts.c.delivery_mode).where(
                tables.invoice_drafts.c.id == draft_id
            )
        )


@pytest.mark.asyncio
async def test_resubmit_can_change_delivery_mode_while_open(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant, intent = uuid4(), uuid4()
    first = await module.submit_draft.execute(
        draft_cmd(merchant, "mode-change", intent, delivery_mode=DeliveryMode.AUTO)
    )
    assert await draft_delivery_mode(engine, inv_tables, first.draft_id) == "auto"
    second = await module.submit_draft.execute(
        draft_cmd(merchant, "mode-change", intent, delivery_mode=DeliveryMode.MANUAL)
    )
    assert (second.draft_id, second.revision) == (first.draft_id, 2)
    assert await draft_delivery_mode(engine, inv_tables, first.draft_id) == "manual"


@pytest.mark.asyncio
async def test_reopen_records_delivery_mode(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    merchant = uuid4()
    first = await module.submit_draft.execute(
        draft_cmd(merchant, "reopen-mode", uuid4(), delivery_mode=DeliveryMode.AUTO)
    )
    await module.discard_draft.execute("tenant-test", "order", "reopen-mode", "actor")
    async with module.uow_factory() as uow:
        reopened = await uow.drafts.reopen(
            first.draft_id, uuid4(), 3, datetime.now(UTC), DeliveryMode.MANUAL
        )
        await uow.commit()
    assert reopened.delivery_mode is DeliveryMode.MANUAL
    assert await draft_delivery_mode(engine, inv_tables, first.draft_id) == "manual"
