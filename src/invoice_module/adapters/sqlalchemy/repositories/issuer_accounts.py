from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.enums import Environment


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
