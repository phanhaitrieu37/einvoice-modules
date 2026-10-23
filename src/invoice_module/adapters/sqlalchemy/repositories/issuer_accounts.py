from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.enums import CrossDayAction, Environment
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


class IssuerAccountRepository(SqlAlchemyRepository):
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
