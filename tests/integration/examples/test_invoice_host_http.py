import asyncio
import json

import httpx
import pytest
import sqlalchemy as sa
from examples.invoice_host.main import (
    ACCOUNT_NUMBER,
    SYNTHETIC_ISSUER_CONFIG,
    SYNTHETIC_LINES,
    build,
    onboard,
    place_order,
    run_until_issued,
)
from examples.invoice_host.server import DEFAULT_HOST, DEFAULT_PORT, create_app, serve
from examples.invoice_host.sign_webhook import signed_delivery
from payment_module.adapters.sepay.provider import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
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


async def test_signed_webhook_over_http_settles_and_issues(pg_url, tmp_path, environ, host_tables):
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(host_tables.metadata.create_all)
        host = build(engine, fake_provider(), tmp_path / "artifacts", tables=host_tables,
                     environ=environ)  # fmt: skip
        onboarded = await onboard(
            host.payment,
            host.invoice,
            SYNTHETIC_ISSUER_CONFIG,
            provider_account_id="synthetic-account",
        )
        placed = await place_order(
            host, onboarded, "order-http", SYNTHETIC_LINES, delivery_mode=DeliveryMode.AUTO
        )
        code = placed.intent.payment_reference
        raw, headers, bank_reference = signed_delivery(
            environ["SEPAY_WEBHOOK_SECRET"], ACCOUNT_NUMBER, placed.intent.amount.value, code, code
        )
        transport = httpx.ASGITransport(app=create_app(host.payment))
        async with httpx.AsyncClient(transport=transport, base_url="http://host.test") as client:
            response = await client.post(
                f"/webhooks/sepay/{onboarded.connection.locator}", content=raw, headers=headers
            )
        assert response.status_code == 200
        await run_until_issued(host, "order-http")
        [view] = await host.invoice.get_document_status.by_host_ref("shop", "order", "order-http")
        assert view.status == "issued"
        assert view.pdf_uri and view.xml_uri and view.invoice_number
        settlements = host_tables.payment.settlements
        transactions = host_tables.payment.provider_transactions
        async with engine.connect() as conn:
            stored = await conn.scalar(
                sa.select(transactions.c.bank_reference)
                .join(settlements, settlements.c.transaction_id == transactions.c.id)
                .where(settlements.c.id == view.settlement_id)
            )
        assert stored == bank_reference
        assert stored.startswith("LOCAL-")
    finally:
        await engine.dispose()


def test_signed_delivery_uses_reference_prefix():
    secret = "synthetic-webhook-secret"
    raw, headers, bank_reference = signed_delivery(secret, ACCOUNT_NUMBER, 110_000, "BILL1", "x")
    body = json.loads(raw)
    assert bank_reference.startswith("LOCAL-")
    assert body["referenceCode"] == bank_reference
    assert (body["accountNumber"], body["transferAmount"], body["code"]) == (
        ACCOUNT_NUMBER,
        110_000,
        "BILL1",
    )
    timestamp = int(headers[TIMESTAMP_HEADER])
    assert headers[SIGNATURE_HEADER] == sign(raw, secret, timestamp)
    _, _, other = signed_delivery(
        secret, ACCOUNT_NUMBER, 1, None, "x", reference_code_prefix="SEPAYTEST-"
    )
    assert other.startswith("SEPAYTEST-")
    assert (
        json.loads(signed_delivery(secret, ACCOUNT_NUMBER, 1, None, "x")[0])["referenceCode"]
        != body["referenceCode"]
    )


async def test_healthz(pg_url, tmp_path, environ, host_tables):
    engine = create_async_engine(pg_url)
    try:
        host = build(engine, fake_provider(), tmp_path, tables=host_tables, environ=environ)
        app = create_app(host.payment)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://host.test") as client:
            response = await client.get("/healthz")
        assert (response.status_code, response.json()) == (200, {"status": "ok"})
        # The same app behind the real listener on its fixed local port, then shut down.
        url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/healthz"
        stop = asyncio.Event()
        server = asyncio.create_task(serve(app, stop))
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        response = await client.get(url)
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
            assert (response.status_code, response.json()) == (200, {"status": "ok"})
        finally:
            stop.set()
            await server
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(url)
    finally:
        await engine.dispose()
