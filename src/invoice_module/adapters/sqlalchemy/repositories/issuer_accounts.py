from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.enums import CrossDayAction, Environment, IssuerAccountStatus
from invoice_module.ports.provider import IssuerAccountView as ProviderAccountView


@dataclass(frozen=True)
class IssuerAccountView:
    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: Environment
    payment_method: str
    buyer_fallback_name: str
    buyer_fallback_address: str
    buyer_fallback_extra: dict[str, str] | None
    issue_date_cross_day_action: str | None


@dataclass(frozen=True)
class IssuerAccountRecord:
    """Every stored column of an issuer account, for onboarding and its status changes."""

    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: Environment
    provider_account_id: str
    template_code: str
    invoice_series_template: str
    payment_method: str
    credential_ref: str
    seller_store_xid: str | None
    buyer_fallback_name: str
    buyer_fallback_address: str
    buyer_fallback_extra: dict[str, str] | None
    issue_date_cross_day_action: CrossDayAction | None
    status: IssuerAccountStatus
    sandbox_evidence_ref: str | None


def _record(row: Any) -> IssuerAccountRecord:
    values = {name: row[name] for name in IssuerAccountRecord.__dataclass_fields__}
    values["environment"] = Environment(values["environment"])
    values["status"] = IssuerAccountStatus(values["status"])
    action = values["issue_date_cross_day_action"]
    values["issue_date_cross_day_action"] = CrossDayAction(action) if action is not None else None
    return IssuerAccountRecord(**values)


class IssuerAccountRepository(SqlAlchemyRepository):
    async def get_record(
        self, account_id: UUID, *, for_update: bool = False
    ) -> IssuerAccountRecord | None:
        table = self.tables.issuer_accounts
        statement = sa.select(table).where(table.c.id == account_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).mappings().one_or_none()
        return _record(row) if row is not None else None

    async def get_by_scope(
        self, tenant_id: str, merchant_id: UUID, environment: Environment
    ) -> IssuerAccountRecord | None:
        """The one account of a merchant and environment, whatever its status."""
        table = self.tables.issuer_accounts
        result = await self.session.execute(
            sa.select(table).where(
                table.c.tenant_id == tenant_id,
                table.c.merchant_id == merchant_id,
                table.c.environment == Environment(environment).value,
            )
        )
        row = result.mappings().one_or_none()
        return _record(row) if row is not None else None

    async def insert_once(self, values: dict[str, Any]) -> bool:
        """Insert the account unless its merchant and environment already have one."""
        table = self.tables.issuer_accounts
        statement = (
            insert(table)
            .values(**values)
            .on_conflict_do_nothing(constraint=f"{self.tables.prefix}issuer_accounts_scope_uq")
            .returning(table.c.id)
        )
        return (await self.session.execute(statement)).first() is not None

    async def set_status(
        self, account_id: UUID, status: IssuerAccountStatus, now: datetime
    ) -> None:
        table = self.tables.issuer_accounts
        await self.session.execute(
            sa.update(table)
            .where(table.c.id == account_id)
            .values(status=IssuerAccountStatus(status).value, updated_at=now)
        )

    async def record_evidence(self, account_id: UUID, evidence_ref: str, now: datetime) -> None:
        table = self.tables.issuer_accounts
        await self.session.execute(
            sa.update(table)
            .where(table.c.id == account_id)
            .values(sandbox_evidence_ref=evidence_ref, updated_at=now)
        )

    async def get_active(
        self, tenant_id: str, merchant_id: UUID, environment: Environment
    ) -> IssuerAccountView | None:
        table = self.tables.issuer_accounts
        result = await self.session.execute(
            sa.select(table).where(
                table.c.tenant_id == tenant_id,
                table.c.merchant_id == merchant_id,
                table.c.environment == getattr(environment, "value", environment),
                table.c.status == "active",
            )
        )
        row = result.mappings().one_or_none()
        return (
            IssuerAccountView(
                **{name: row[name] for name in IssuerAccountView.__dataclass_fields__}
            )
            if row is not None
            else None
        )

    async def get_view(self, account_id: UUID) -> ProviderAccountView | None:
        """The account as the provider port sees it (any status: a document keeps its account)."""
        table = self.tables.issuer_accounts
        result = await self.session.execute(sa.select(table).where(table.c.id == account_id))
        row = result.mappings().one_or_none()
        if row is None:
            return None
        action = row["issue_date_cross_day_action"]
        return ProviderAccountView(
            id=row["id"],
            tenant_id=row["tenant_id"],
            merchant_id=row["merchant_id"],
            environment=Environment(row["environment"]),
            provider_account_id=row["provider_account_id"],
            template_code=row["template_code"],
            invoice_series_template=row["invoice_series_template"],
            payment_method=row["payment_method"],
            seller_store_xid=row["seller_store_xid"],
            credential_ref=row["credential_ref"],
            buyer_fallback_name=row["buyer_fallback_name"],
            buyer_fallback_address=row["buyer_fallback_address"],
            buyer_fallback_extra=row["buyer_fallback_extra"],
            issue_date_cross_day_action=CrossDayAction(action) if action is not None else None,
        )
