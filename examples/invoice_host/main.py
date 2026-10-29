"""Example host: payment-module settles an order, invoice-module issues its VAT invoice.

One database holds the host's orders, the payment tables (``pm_``) and the invoice tables
(``inv_``). Placing an order creates the payment intent and submits the invoice draft in one
transaction; the outbox fans ``PaymentSettled`` out to the host's own consumer and to the
invoice consumer, in that order.

Smoke with ``DATABASE_URL`` (asyncpg), ``SEPAY_WEBHOOK_SECRET`` and the eInvoice credential
variables named by ``env:SEPAY_EINVOICE``: ``smoke(provider, artifact_root)`` returns a dict.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import UUID

import sqlalchemy as sa
from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.adapters.sqlalchemy.tables import PaymentTables
from payment_module.adapters.sqlalchemy.tables import define_tables as define_payment_tables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork, SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.create_intent import CreateIntentCommand, CreateIntentResult
from payment_module.builder import PaymentModule, build_payment_module
from payment_module.domain.enums import ConnectionStatus
from payment_module.domain.enums import Environment as PaymentEnvironment
from payment_module.ports.clock import Clock, SystemClock
from payment_module.secrets import EnvSecretResolver
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from invoice_module.adapters.sqlalchemy.repositories.issuer_accounts import IssuerAccountRecord
from invoice_module.adapters.sqlalchemy.tables import InvoiceTables
from invoice_module.adapters.sqlalchemy.tables import define_tables as define_invoice_tables
from invoice_module.adapters.sqlalchemy.uow import (
    SqlAlchemyInvoiceUnitOfWork,
    SqlAlchemyInvoiceUnitOfWorkFactory,
)
from invoice_module.adapters.storage import FileArtifactStore
from invoice_module.application.issuer_accounts import RegisterIssuerAccountCommand
from invoice_module.application.submit_draft import SubmitDraftCommand, build_line_inputs
from invoice_module.builder import InvoiceModule, build_invoice_module
from invoice_module.domain.enums import DeliveryMode, IssuerAccountStatus
from invoice_module.domain.enums import Environment as InvoiceEnvironment
from invoice_module.domain.errors import DocumentInFlight
from invoice_module.domain.money import compute_total
from invoice_module.integrations.payment_module import FanOutPublisher, PaymentSettledConsumer
from invoice_module.ports.provider import EInvoiceProvider
from invoice_module.secrets import EnvCredentialResolver

from .order_consumer import OrderConsumer, OrderTables, define_order_tables
from .server import run_workers
from .sign_webhook import signed_delivery

TENANT, ADMIN, ACCOUNT_NUMBER = "shop", "admin@shop", "0071000654321"
ORDER = "order"
WEBHOOK_SECRET_REF = "env:SEPAY_WEBHOOK_SECRET"
EINVOICE_CREDENTIAL_REF = "env:SEPAY_EINVOICE"
SMOKE_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class HostTables:
    metadata: sa.MetaData
    orders: OrderTables
    payment: PaymentTables
    invoice: InvoiceTables


def define_host_tables(prefix: str = "") -> HostTables:
    """Every table of the host on one ``MetaData``; ``prefix`` keeps test runs apart."""
    metadata = sa.MetaData()
    return HostTables(
        metadata,
        define_order_tables(metadata, prefix),
        define_payment_tables(metadata, f"{prefix}pm_"),
        define_invoice_tables(metadata, f"{prefix}inv_"),
    )


TABLES = define_host_tables()


@dataclass(frozen=True)
class Host:
    engine: AsyncEngine
    tables: HostTables
    payment: PaymentModule
    invoice: InvoiceModule


@dataclass(frozen=True)
class Onboarded:
    merchant_id: UUID
    receiving_account_id: UUID
    connection: Any
    issuer_account: IssuerAccountRecord


def build(
    engine: AsyncEngine,
    provider: EInvoiceProvider,
    artifact_root: Path,
    *,
    tables: HostTables = TABLES,
    clock: Clock | None = None,
    environ: Mapping[str, str] | None = None,
) -> Host:
    """Both modules on one engine; secrets and credentials come from ``environ`` (default:
    the process environment), always through ``env:NAME`` references."""
    clock = clock or SystemClock()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    invoice = build_invoice_module(
        SqlAlchemyInvoiceUnitOfWorkFactory(sessions, tables.invoice),
        clock,
        provider=provider,
        credentials=EnvCredentialResolver(environ),
        artifact_store=FileArtifactStore(artifact_root),
    )
    publisher = FanOutPublisher(
        [
            OrderConsumer(engine, tables.orders),
            PaymentSettledConsumer(invoice.record_settled_payment),
        ]
    )
    payment = build_payment_module(
        PaymentModuleConfig(),
        SqlAlchemyUnitOfWorkFactory(sessions, tables.payment),
        {"sepay": SePayProvider()},
        EnvSecretResolver(environ=environ),
        clock,
        outbox_publisher=publisher,
        template_checklists={"sepay": SePayTemplateChecklist()},
    )
    return Host(engine, tables, payment, invoice)


async def onboard(
    payment: PaymentModule,
    invoice: InvoiceModule,
    issuer_config: Mapping[str, Any],
    *,
    provider_account_id: str,
    credential_ref: str = EINVOICE_CREDENTIAL_REF,
) -> Onboarded:
    """Payment side in the order readiness requires, then the issuer account (test).

    ``issuer_config`` has the shape of the accountant's configuration file
    (``template_code``, ``invoice_series_template``, ``payment_method``,
    ``buyer_fallback_name``, ``buyer_fallback_address``, ``issue_date_cross_day_action``).
    """
    test = PaymentEnvironment.TEST
    merchant = await payment.register_merchant.execute(TENANT, "branch-1", ADMIN)
    account = await payment.register_receiving_account.execute(
        TENANT, merchant.id, test, "VCB", ACCOUNT_NUMBER, "CONG TY SHOP", ADMIN
    )
    connection = await payment.register_connection.execute(
        TENANT, merchant.id, "sepay", test, WEBHOOK_SECRET_REF, ADMIN
    )
    await payment.bind_connection_account.execute(TENANT, connection.id, account.id, ADMIN)
    draft = await payment.create_reference_profile.execute(
        1, {"bill": "BILL"}, 6, "23456789", ADMIN
    )
    done = {
        item.key: True
        for item in SePayTemplateChecklist().checklist(connection, draft.profile).items
    }
    await payment.record_connection_readiness.execute(
        TENANT, connection.id, 1, test, done, "local-example", ADMIN
    )
    await payment.activate_reference_profile.execute(1, ADMIN)
    await payment.set_connection_status.execute(
        TENANT, connection.id, ConnectionStatus.ACTIVE, ADMIN, "example"
    )
    assert invoice.register_issuer_account is not None, "the invoice module needs a provider"
    issuer = await invoice.register_issuer_account.execute(
        RegisterIssuerAccountCommand(
            tenant_id=TENANT,
            merchant_id=merchant.id,
            environment=InvoiceEnvironment.TEST,
            provider_account_id=provider_account_id,
            template_code=issuer_config["template_code"],
            invoice_series_template=issuer_config["invoice_series_template"],
            payment_method=issuer_config["payment_method"],
            credential_ref=credential_ref,
            buyer_fallback_name=issuer_config["buyer_fallback_name"],
            buyer_fallback_address=issuer_config["buyer_fallback_address"],
            issue_date_cross_day_action=issuer_config["issue_date_cross_day_action"],
        ),
        ADMIN,
    )
    issuer = await invoice.set_issuer_account_status.execute(
        issuer.id, IssuerAccountStatus.ACTIVE, ADMIN, "example onboarding"
    )
    return Onboarded(merchant.id, account.id, connection, issuer)


async def place_order(
    host: Host,
    onboarded: Onboarded,
    order_id: str,
    lines: Sequence[Mapping[str, Any]],
    *,
    delivery_mode: DeliveryMode,
) -> CreateIntentResult:
    """The order row, its payment intent and its invoice draft commit together or not at all.

    The bill amount is the invoice total of ``lines`` (tax included), so the settled amount
    and the original invoice always match.
    """
    amount_vnd = compute_total(build_line_inputs(lines))
    sessions = async_sessionmaker(host.engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await session.execute(
            sa.insert(host.tables.orders.orders).values(id=order_id, amount_vnd=amount_vnd)
        )
        created = await host.payment.create_intent.execute(
            CreateIntentCommand(
                TENANT,
                amount_vnd=amount_vnd,
                host_ref_type=ORDER,
                host_ref_id=order_id,
                idempotency_key=order_id,
                expires_at=host.payment.clock.now() + timedelta(minutes=30),
                prefix_name="bill",
                merchant_id=onboarded.merchant_id,
                receiving_account_id=onboarded.receiving_account_id,
            ),
            uow=SqlAlchemyUnitOfWork.joined(session, host.tables.payment),
        )
        await host.invoice.submit_draft.execute(
            SubmitDraftCommand(
                TENANT,
                onboarded.merchant_id,
                ORDER,
                order_id,
                created.intent.id,
                tuple(lines),
                amount_vnd,
                delivery_mode=delivery_mode,
            ),
            uow=SqlAlchemyInvoiceUnitOfWork.joined(session, host.tables.invoice),
        )
    return created


# Synthetic values for the smoke; a real host passes the accountant's configuration.
SYNTHETIC_ISSUER_CONFIG: Mapping[str, Any] = {
    "issue_date_cross_day_action": "issue",
    "template_code": "1",
    "invoice_series_template": "C{YY}TSE",
    "payment_method": "TM/CK",
    "buyer_fallback_name": "Synthetic fallback buyer",
    "buyer_fallback_address": "",
}
SYNTHETIC_LINES: tuple[Mapping[str, Any], ...] = (
    {
        "name": "Synthetic service",
        "unit": "month",
        "quantity": Decimal("1"),
        "unit_price_vnd": 100_000,
        "tax_rate": 10,
        "item_code": "SYN-ITEM-1",
    },
)


async def smoke(
    provider: EInvoiceProvider,
    artifact_root: Path,
    *,
    delivery_mode: DeliveryMode = DeliveryMode.AUTO,
    approver: str = "accountant@shop",
    database_url: str | None = None,
    tables: HostTables = TABLES,
    environ: Mapping[str, str] | None = None,
    issuer_config: Mapping[str, Any] = SYNTHETIC_ISSUER_CONFIG,
    provider_account_id: str = "synthetic-account",
    order_id: str = "order-1",
) -> dict[str, Any]:
    """Signed webhook -> payment worker -> invoice worker, until the order's invoice is
    issued with its PDF and XML (at most 60 s). A ``manual`` order is approved by
    ``approver`` once it waits for approval."""
    env = os.environ if environ is None else environ
    engine = create_async_engine(database_url or env["DATABASE_URL"])
    try:
        async with engine.begin() as db:
            await db.run_sync(tables.metadata.create_all)
        host = build(engine, provider, artifact_root, tables=tables, environ=environ)
        onboarded = await onboard(
            host.payment, host.invoice, issuer_config, provider_account_id=provider_account_id
        )
        placed = await place_order(
            host, onboarded, order_id, SYNTHETIC_LINES, delivery_mode=delivery_mode
        )
        code = placed.intent.payment_reference
        raw, headers, bank_reference = signed_delivery(
            env["SEPAY_WEBHOOK_SECRET"], ACCOUNT_NUMBER, placed.intent.amount.value, code, code
        )
        await host.payment.ingest_webhook.execute(onboarded.connection.locator, raw, headers)
        observed = await run_until_issued(host, order_id, approver)
        return await _report(host, order_id, bank_reference) | observed
    finally:
        await engine.dispose()


async def run_until_issued(
    host: Host, order_id: str, approver: str | None = None
) -> dict[str, Any]:
    """Run both workers until the order's invoice is issued with its PDF and XML (at most
    60 s); a ``manual`` draft is approved by ``approver`` once it waits for approval.

    Returns what was seen on the way: when the draft was approved and its status then.
    """
    stop = asyncio.Event()
    workers = asyncio.create_task(run_workers(host.payment, host.invoice, stop, fast=True))
    observed: dict[str, Any] = {"approved_at": None, "status_before_approval": None}
    try:
        async with asyncio.timeout(SMOKE_TIMEOUT_S):
            while True:
                views = await host.invoice.get_document_status.by_host_ref(TENANT, ORDER, order_id)
                view = views[0] if views else None
                if view is not None and view.status == "awaiting_approval" and approver:
                    now = host.invoice.clock.now()
                    with contextlib.suppress(DocumentInFlight):  # a worker step holds it now
                        await host.invoice.approve_draft_invoice.execute(view.document_id, approver)
                        observed |= {
                            "approved_at": now.isoformat(),
                            "status_before_approval": view.status.value,
                        }
                if view is not None and view.status == "issued" and view.pdf_uri and view.xml_uri:
                    return observed
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await workers


async def _report(host: Host, order_id: str, bank_reference: str) -> dict[str, Any]:
    documents = host.tables.invoice.documents
    receipts = host.tables.invoice.settlement_receipts
    orders = host.tables.orders
    async with host.engine.connect() as db:
        originals = (
            await db.execute(
                sa.select(documents).where(
                    documents.c.host_ref_type == ORDER,
                    documents.c.host_ref_id == order_id,
                    documents.c.kind == "original",
                )
            )
        ).all()
        doc = originals[0]
        receipt_count = await db.scalar(
            sa.select(sa.func.count()).where(receipts.c.settlement_id == doc.settlement_id)
        )
        paid_by = await db.scalar(
            sa.select(orders.orders.c.paid_by_event_id).where(orders.orders.c.id == order_id)
        )
        order_receipt = await db.scalar(
            sa.select(orders.order_receipts.c.event_id).where(
                orders.order_receipts.c.order_id == order_id
            )
        )
    return {
        "document_id": str(doc.id),
        "settlement_id": str(doc.settlement_id),
        "receipts": receipt_count,
        "documents_original": len(originals),
        "status": doc.status,
        "total_vnd": doc.total_vnd,
        "settled_amount_vnd": doc.settled_amount_vnd,
        "pdf_sha256": doc.pdf_sha256,
        "xml_sha256": doc.xml_sha256,
        "pdf_bytes_sha256": _file_sha256(doc.pdf_uri),
        "order_paid": paid_by is not None and paid_by == order_receipt,
        "approved_by": doc.approved_by,
        "settled_at": doc.settled_at.isoformat(),
        "bank_reference": bank_reference,
    }


def _file_sha256(uri: str | None) -> str | None:
    if not uri:
        return None
    return hashlib.sha256(Path(url2pathname(urlparse(uri).path)).read_bytes()).hexdigest()
