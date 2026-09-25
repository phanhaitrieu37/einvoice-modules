from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.enums import DocumentStatus


@dataclass(frozen=True)
class DocumentRow:
    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: str
    settlement_id: UUID
    intent_id: UUID
    host_ref_type: str
    host_ref_id: str
    issuer_account_id: UUID | None
    draft_id: UUID | None
    settled_amount_vnd: int
    settled_at: datetime
    status: DocumentStatus
    document: dict[str, Any] | None


class DocumentRepository(SqlAlchemyRepository):
    async def insert(self, row: dict[str, Any]) -> DocumentRow:
        table = self.tables.documents
        result = await self.session.execute(table.insert().values(**row).returning(table))
        result_row = result.mappings().one()
        return DocumentRow(**{name: result_row[name] for name in DocumentRow.__dataclass_fields__})

    async def get_original(self, tenant_id: str, settlement_id: UUID) -> DocumentRow | None:
        table = self.tables.documents
        result = await self.session.execute(
            sa.select(table).where(
                table.c.tenant_id == tenant_id,
                table.c.settlement_id == settlement_id,
                table.c.kind == "original",
            )
        )
        row = result.mappings().one_or_none()
        return (
            DocumentRow(**{name: row[name] for name in DocumentRow.__dataclass_fields__})
            if row is not None
            else None
        )

    async def exists_for_intent(self, tenant_id: str, intent_id: UUID) -> bool:
        table = self.tables.documents
        value = await self.session.scalar(
            sa.select(
                sa.exists().where(table.c.tenant_id == tenant_id, table.c.intent_id == intent_id)
            )
        )
        return bool(value)

    async def find_awaiting_draft(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str
    ) -> list[DocumentRow]:
        table, reviews = self.tables.documents, self.tables.review_cases
        open_mismatch = sa.exists(
            sa.select(1).where(
                reviews.c.document_id == table.c.id,
                reviews.c.status == "open",
                sa.cast(reviews.c.details, postgresql.JSONB).contains(
                    {"reasons": [{"reason": "AMOUNT_MISMATCH", "status": "open"}]}
                ),
            )
        )
        result = await self.session.execute(
            sa.select(table).where(
                table.c.tenant_id == tenant_id,
                table.c.host_ref_type == host_ref_type,
                table.c.host_ref_id == host_ref_id,
                sa.or_(
                    table.c.status == DocumentStatus.NEEDS_DRAFT.value,
                    sa.and_(table.c.status == DocumentStatus.BLOCKED.value, open_mismatch),
                ),
            )
        )
        return [
            DocumentRow(**{name: row[name] for name in DocumentRow.__dataclass_fields__})
            for row in result.mappings()
        ]

    async def has_other_original_for_host_ref(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str, settlement_id: UUID
    ) -> list[UUID]:
        table = self.tables.documents
        result = await self.session.scalars(
            sa.select(table.c.settlement_id).where(
                table.c.tenant_id == tenant_id,
                table.c.host_ref_type == host_ref_type,
                table.c.host_ref_id == host_ref_id,
                table.c.kind == "original",
                table.c.settlement_id != settlement_id,
            )
        )
        return list(result)

    async def update_frozen(
        self,
        document_id: UUID,
        snapshot: dict[str, Any] | None,
        total_vnd: int | None,
        buyer_source: str | None,
        status: str,
        queued_at: datetime,
        next_attempt_at: datetime,
        draft_id: UUID,
    ) -> None:
        table = self.tables.documents
        await self.session.execute(
            table.update()
            .where(table.c.id == document_id)
            .values(
                document=snapshot,
                total_vnd=total_vnd,
                buyer_source=buyer_source,
                status=status,
                queued_at=queued_at,
                next_attempt_at=next_attempt_at,
                draft_id=draft_id,
            )
        )
