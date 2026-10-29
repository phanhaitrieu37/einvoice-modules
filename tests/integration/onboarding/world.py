"""An onboarding world on real PostgreSQL: the module with the fake provider and a fake clock."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider
from tests.fakes.recording import FakeClock, StaticCredentials

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables
from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWorkFactory
from invoice_module.application.issuer_accounts import RegisterIssuerAccountCommand
from invoice_module.builder import InvoiceModule, build_invoice_module
from invoice_module.domain.enums import CrossDayAction, Environment

# Thursday 29 October 2026, 10:00 in Asia/Saigon.
NOW = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
TENANT = "tenant-test"
ACTOR = "operator-synthetic"


class OnboardingWorld:
    def __init__(self, engine: AsyncEngine, tables: InvoiceTables) -> None:
        self.engine, self.tables = engine, tables
        self.clock = FakeClock(NOW)
        self.provider = FakeEInvoiceProvider(self.clock)
        self.credentials = StaticCredentials()
        self.uow_factory = SqlAlchemyInvoiceUnitOfWorkFactory(
            async_sessionmaker(engine, expire_on_commit=False), tables
        )
        self.module = self.build(self.provider)

    def build(self, provider: Any) -> InvoiceModule:
        return build_invoice_module(
            self.uow_factory, self.clock, provider=provider, credentials=self.credentials
        )

    def command(self, **over: Any) -> RegisterIssuerAccountCommand:
        """Synthetic accounting settings shaped like the accountant's configuration file."""
        values: dict[str, Any] = {
            "tenant_id": TENANT,
            "merchant_id": uuid4(),
            "environment": Environment.TEST,
            "provider_account_id": "synthetic-account",
            "template_code": "1",
            "invoice_series_template": "C{YY}TSE",
            "payment_method": "TM/CK",
            "credential_ref": "env:SYNTHETIC_CREDENTIAL",
            "buyer_fallback_name": "Synthetic fallback buyer",
            "buyer_fallback_address": "",
            "issue_date_cross_day_action": CrossDayAction.ISSUE,
        } | over
        return RegisterIssuerAccountCommand(**values)

    async def register(self, **over: Any):
        return await self.module.register_issuer_account.execute(self.command(**over), ACTOR)

    async def rows(self) -> list[dict[str, Any]]:
        table = self.tables.issuer_accounts
        async with self.engine.connect() as conn:
            result = await conn.execute(sa.select(table).order_by(table.c.created_at))
            return [dict(row) for row in result.mappings()]

    async def row(self, account_id) -> dict[str, Any]:
        table = self.tables.issuer_accounts
        async with self.engine.connect() as conn:
            result = await conn.execute(sa.select(table).where(table.c.id == account_id))
            return dict(result.mappings().one())

    def provider_calls(self, op: str = "describe_account") -> int:
        return sum(1 for call in self.provider.calls if call.op == op)
