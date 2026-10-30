import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
import uvicorn
from examples.invoice_host import server as server_module
from examples.invoice_host.main import (
    ACCOUNT_NUMBER,
    SYNTHETIC_ISSUER_CONFIG,
    SYNTHETIC_LINES,
    build,
    onboard,
    place_order,
)
from examples.invoice_host.order_consumer import OrderConsumer, OrderNotFound
from examples.invoice_host.server import DEFAULT_HOST, DEFAULT_PORT, serve
from examples.invoice_host.sign_webhook import signed_delivery
from fastapi import FastAPI
from payment_module.domain.enums import OutboxStatus
from payment_module.ports.publisher import OutboxEventView
from sqlalchemy.ext.asyncio import create_async_engine
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider

from invoice_module.application.config import today_vn
from invoice_module.domain.enums import DeliveryMode
from invoice_module.ports.clock import SystemClock

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def fake_provider() -> FakeEInvoiceProvider:
    clock = SystemClock()
    provider = FakeEInvoiceProvider(clock)
    provider.set_account_facts(series=(f"C{today_vn(clock.now()).year % 100:02d}TSE",))
    return provider


def settled_event(order_id: str) -> OutboxEventView:
    return OutboxEventView(
        uuid4(), "PaymentSettled", 1, {}, {"host_ref_id": order_id}, datetime.now(UTC)
    )


@pytest.fixture
async def host(pg_url, tmp_path, environ, host_tables):
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(host_tables.metadata.create_all)
        yield build(engine, fake_provider(), tmp_path / "artifacts", tables=host_tables,
                    environ=environ)  # fmt: skip
    finally:
        await engine.dispose()


async def order_state(host, order_id: str) -> tuple[list, object]:
    orders = host.tables.orders
    async with host.engine.connect() as db:
        receipts = (
            (
                await db.execute(
                    sa.select(orders.order_receipts.c.event_id).where(
                        orders.order_receipts.c.order_id == order_id
                    )
                )
            )
            .scalars()
            .all()
        )
        paid_by = await db.scalar(
            sa.select(orders.orders.c.paid_by_event_id).where(orders.orders.c.id == order_id)
        )
    return list(receipts), paid_by


async def add_order(host, order_id: str) -> None:
    async with host.engine.begin() as db:
        await db.execute(sa.insert(host.tables.orders.orders).values(id=order_id, amount_vnd=1))


async def test_redelivered_settlement_marks_the_order_paid_once(host):
    await add_order(host, "order-1")
    consumer = OrderConsumer(host.engine, host.tables.orders)
    event = settled_event("order-1")
    await consumer.publish(event)
    await consumer.publish(event)  # the outbox delivers at least once
    assert await order_state(host, "order-1") == ([event.event_id], event.event_id)


async def test_unknown_order_rolls_back_its_receipt(host):
    consumer = OrderConsumer(host.engine, host.tables.orders)
    event = settled_event("order-unknown")
    with pytest.raises(OrderNotFound):
        await consumer.publish(event)
    assert await order_state(host, "order-unknown") == ([], None)
    # Once the order exists, the next delivery of the same event marks it paid.
    await add_order(host, "order-unknown")
    await consumer.publish(event)
    assert await order_state(host, "order-unknown") == ([event.event_id], event.event_id)


async def test_host_consumer_runs_before_the_invoice_consumer(host, environ):
    onboarded = await onboard(
        host.payment, host.invoice, SYNTHETIC_ISSUER_CONFIG, provider_account_id="synthetic-account"
    )
    placed = await place_order(
        host, onboarded, "order-gone", SYNTHETIC_LINES, delivery_mode=DeliveryMode.AUTO
    )
    async with host.engine.begin() as db:
        orders = host.tables.orders.orders
        await db.execute(sa.delete(orders).where(orders.c.id == "order-gone"))
    code = placed.intent.payment_reference
    raw, headers, _ = signed_delivery(
        environ["SEPAY_WEBHOOK_SECRET"], ACCOUNT_NUMBER, placed.intent.amount.value, code, code
    )
    await host.payment.ingest_webhook.execute(onboarded.connection.locator, raw, headers)
    await host.payment.process_inbox.run_batch()
    [result] = await host.payment.dispatch_outbox.run_batch()
    # The host's consumer refused the event first, so the invoice side recorded nothing and
    # the outbox delivers the event again later.
    assert result.status == OutboxStatus.PENDING
    receipts = host.tables.invoice.settlement_receipts
    async with host.engine.connect() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(receipts)) == 0
    assert await host.invoice.get_document_status.by_host_ref("shop", "order", "order-gone") == []


async def test_listener_binds_only_the_local_address(monkeypatch):
    started: list[uvicorn.Server] = []

    class RecordingServer(uvicorn.Server):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            started.append(self)

    monkeypatch.setattr(server_module.uvicorn, "Server", RecordingServer)
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    stop = asyncio.Event()
    task = asyncio.create_task(serve(app, stop))
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    response = await client.get(f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/healthz")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
        assert response.status_code == 200
        [listener] = started
        bound = {sock.getsockname()[:2] for srv in listener.servers for sock in srv.sockets}
        assert bound == {("127.0.0.1", 8765)}
    finally:
        stop.set()
        await task
