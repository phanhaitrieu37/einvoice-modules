from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.application.create_intent import CreateIntentCommand
from payment_module.ports.publisher import OutboxEventView
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.support.invoice_world import build_invoice, sample_lines, seed_issuer_account
from tests.support.payment_world import build_payment, onboard_payment, pm_tables, settle

from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWork
from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.domain.errors import ScopeMismatch, UnsupportedEventVersion
from invoice_module.integrations.payment_module import FanOutPublisher, PaymentSettledConsumer

pytestmark = pytest.mark.integration


def outbox_event(**over):
    event_id = uuid4()
    merchant = uuid4()
    scope = {"tenant_id": "tenant-test", "merchant_id": str(merchant), "environment": "test"}
    payload = {
        "event_id": str(event_id),
        "tenant_id": "tenant-test",
        "merchant_id": str(merchant),
        "environment": "test",
        "intent_id": str(uuid4()),
        "transaction_id": str(uuid4()),
        "settlement_id": str(uuid4()),
        "amount_vnd": 100,
        "origin": "auto",
        "settled_at": datetime.now(UTC).isoformat(),
        "host_ref_type": "order",
        "host_ref_id": "order-test",
        "trusted_scope": scope,
    } | over
    return OutboxEventView(event_id, "PaymentSettled", 1, scope, payload, datetime.now(UTC))


class Recording:
    def __init__(self, order, name, fail=False):
        self.order, self.name, self.fail = order, name, fail

    async def publish(self, event):
        self.order.append((self.name, event.event_id))
        if self.fail:
            raise RuntimeError("consumer failure")


@pytest.mark.asyncio
async def test_consumer_ignores_other_event_types(engine, inv_tables):
    module = build_invoice(engine, inv_tables)
    event = outbox_event()
    other = OutboxEventView(event.event_id, "PaymentNeedsReview", 999, {}, {}, event.occurred_at)
    await PaymentSettledConsumer(module.record_settled_payment).publish(other)
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                sa.select(sa.func.count()).select_from(inv_tables.settlement_receipts)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_consumer_rejects_unknown_schema_version(engine, inv_tables):
    event = outbox_event()
    event = OutboxEventView(
        event.event_id, event.event_type, 2, event.trusted_scope, event.payload, event.occurred_at
    )
    with pytest.raises(UnsupportedEventVersion):
        await PaymentSettledConsumer(
            build_invoice(engine, inv_tables).record_settled_payment
        ).publish(event)


@pytest.mark.asyncio
async def test_consumer_rejects_bool_schema_version(engine, inv_tables):
    event = outbox_event()
    with pytest.raises(UnsupportedEventVersion):
        await PaymentSettledConsumer(
            build_invoice(engine, inv_tables).record_settled_payment
        ).publish(
            OutboxEventView(
                event.event_id,
                event.event_type,
                True,
                event.trusted_scope,
                event.payload,
                event.occurred_at,
            )
        )


@pytest.mark.asyncio
async def test_consumer_rejects_scope_mismatch(engine, inv_tables):
    event = outbox_event()
    scope = dict(event.trusted_scope) | {"tenant_id": "wrong"}
    with pytest.raises(ScopeMismatch):
        await PaymentSettledConsumer(
            build_invoice(engine, inv_tables).record_settled_payment
        ).publish(
            OutboxEventView(
                event.event_id, event.event_type, 1, scope, event.payload, event.occurred_at
            )
        )


@pytest.mark.asyncio
async def test_consumer_rejects_payload_event_id_mismatch(engine, inv_tables):
    event = outbox_event(event_id=str(uuid4()))
    with pytest.raises(ScopeMismatch, match="event id"):
        await PaymentSettledConsumer(
            build_invoice(engine, inv_tables).record_settled_payment
        ).publish(event)


@pytest.mark.asyncio
async def test_consumer_rejects_payload_trusted_scope_mismatch(engine, inv_tables):
    event = outbox_event()
    payload = dict(event.payload)
    payload["trusted_scope"] = dict(event.trusted_scope) | {"tenant_id": "wrong-tenant"}
    bad_event = OutboxEventView(
        event.event_id,
        event.event_type,
        event.schema_version,
        event.trusted_scope,
        payload,
        event.occurred_at,
    )
    with pytest.raises(ScopeMismatch, match="trusted scope"):
        await PaymentSettledConsumer(
            build_invoice(engine, inv_tables).record_settled_payment
        ).publish(bad_event)


@pytest.mark.asyncio
async def test_consumer_rejects_bool_amount(engine, inv_tables):
    event = outbox_event(amount_vnd=True)
    with pytest.raises(ValueError, match="amount_vnd"):
        await PaymentSettledConsumer(
            build_invoice(engine, inv_tables).record_settled_payment
        ).publish(event)


@pytest.mark.asyncio
async def test_fanout_calls_in_order_and_propagates_failure():
    order = []
    publisher = FanOutPublisher(
        [Recording(order, "host"), Recording(order, "invoice", True), Recording(order, "last")]
    )
    with pytest.raises(RuntimeError, match="consumer failure"):
        await publisher.publish(outbox_event())
    assert [name for name, _ in order] == ["host", "invoice"]


def test_fanout_rejects_empty():
    with pytest.raises(ValueError, match="at least one"):
        FanOutPublisher([])


@pytest.mark.asyncio
async def test_payment_outbox_to_invoice_document(engine, inv_tables):
    order = []
    host = Recording(order, "host")
    invoice = build_invoice(engine, inv_tables)
    pm = pm_tables(f"pm_{uuid4().hex[:8]}_")
    async with engine.begin() as conn:
        await conn.run_sync(pm.merchants.metadata.create_all)
    fanout = FanOutPublisher([host, _OrderedInvoice(order, invoice.record_settled_payment)])
    payment = build_payment(engine, pm, fanout)
    merchant, receiving, connection = await onboard_payment(payment)
    await seed_issuer_account(
        type(
            "Rows",
            (),
            {
                "insert": lambda self, table, values: _insert(engine, table, values),
                "tables": inv_tables,
            },
        )(),
        tenant_id="tenant-test",
        merchant_id=merchant.id,
    )
    host_ref = f"real-outbox-{uuid4()}"
    intent_cmd = CreateIntentCommand(
        "tenant-test",
        amount_vnd=100,
        host_ref_type="order",
        host_ref_id=host_ref,
        idempotency_key=host_ref,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        prefix_name="bill",
        merchant_id=merchant.id,
        receiving_account_id=receiving.id,
    )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session, session.begin():
        created = await payment.create_intent.execute(
            intent_cmd, SqlAlchemyUnitOfWork.joined(session, pm)
        )
        await invoice.submit_draft.execute(
            SubmitDraftCommand(
                "tenant-test",
                merchant.id,
                "order",
                host_ref,
                created.intent.id,
                sample_lines(),
                100,
            ),
            uow=SqlAlchemyInvoiceUnitOfWork.joined(session, inv_tables),
        )
    await settle(payment, connection, created.intent)
    dispatch = await payment.dispatch_outbox.run_batch()
    assert dispatch and [name for name, _ in order] == ["host", "invoice"]
    event_id = order[0][1]
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(
                    inv_tables.settlement_receipts.c.settlement_id,
                    inv_tables.documents.c.reference_code,
                    inv_tables.documents.c.status,
                    inv_tables.documents.c.document,
                )
                .join(
                    inv_tables.documents,
                    inv_tables.documents.c.id == inv_tables.settlement_receipts.c.document_id,
                )
                .where(inv_tables.settlement_receipts.c.event_id == event_id)
            )
        ).one()
    assert row.reference_code == str(row.settlement_id)
    assert row.status == "queued" and row.document["lines"][0]["name"] == "Test service"
    assert order[0][0] == "host"


class _OrderedInvoice:
    def __init__(self, order, record):
        from invoice_module.integrations.payment_module.consumer import PaymentSettledConsumer

        self._order = order
        self._consumer = PaymentSettledConsumer(record)

    async def publish(self, event):
        self._order.append(("invoice", event.event_id))
        await self._consumer.publish(event)


async def _insert(engine, table, values):
    async with engine.begin() as conn:
        await conn.execute(table.insert().values(**values))
    return values


@pytest.mark.asyncio
async def test_outbox_redelivery_three_times_one_document(engine, inv_tables):
    event = outbox_event()
    module = build_invoice(engine, inv_tables)
    consumer = PaymentSettledConsumer(module.record_settled_payment)
    await consumer.publish(event)
    await consumer.publish(event)
    await consumer.publish(event)
    async with engine.connect() as conn:
        docs = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(inv_tables.documents)
            .where(inv_tables.documents.c.settlement_id == event.payload["settlement_id"])
        )
        receipts = await conn.scalar(
            sa.select(sa.func.count())
            .select_from(inv_tables.settlement_receipts)
            .where(inv_tables.settlement_receipts.c.event_id == event.event_id)
        )
    assert docs == 1 and receipts == 1
