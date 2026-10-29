from decimal import Decimal
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables, define_tables
from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWorkFactory
from invoice_module.builder import build_invoice_module
from invoice_module.domain.enums import Environment
from invoice_module.domain.money import LineInput
from invoice_module.ports.clock import SystemClock


def build_invoice(engine: AsyncEngine, tables: InvoiceTables):
    return build_invoice_module(
        SqlAlchemyInvoiceUnitOfWorkFactory(
            async_sessionmaker(engine, expire_on_commit=False), tables
        ),
        SystemClock(),
    )


async def seed_issuer_account(rows: Any, **over: Any) -> dict[str, Any]:
    values = {
        "id": uuid4(),
        "tenant_id": "tenant-test",
        "merchant_id": uuid4(),
        "environment": Environment.TEST.value,
        "provider": "sepay_einvoice",
        "provider_account_id": "test-account",
        "template_code": "1",
        "invoice_series_template": "TEST{YY}",
        "payment_method": "TM",
        "credential_ref": "env:TEST_CREDENTIAL",
        "buyer_fallback_name": "TEST-FALLBACK-NAME",
        "buyer_fallback_address": "TEST-FALLBACK-ADDRESS",
        "issue_date_cross_day_action": "issue",
        "status": "active",
    } | over
    if hasattr(rows, "insert"):
        await rows.insert(rows.tables.issuer_accounts, values)
    return values


def sample_lines() -> tuple[LineInput, ...]:
    return (
        LineInput(
            name="Test service",
            unit="service",
            quantity=Decimal("1"),
            unit_price_vnd=100,
            tax_rate=0,
            item_code="TEST-ITEM-1",
        ),
    )


def create_invoice_tables(prefix: str = "inv_") -> tuple[sa.MetaData, InvoiceTables]:
    metadata = sa.MetaData()
    return metadata, define_tables(metadata, prefix)
