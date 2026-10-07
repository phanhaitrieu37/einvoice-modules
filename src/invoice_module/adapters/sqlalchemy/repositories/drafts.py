from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.enums import DeliveryMode, DraftStatus


@dataclass(frozen=True)
class DraftRow:
    id: UUID
    tenant_id: str
    merchant_id: UUID
    host_ref_type: str
    host_ref_id: str
    intent_id: UUID | None
    buyer: dict[str, Any] | None
    items: list[dict[str, Any]]
    notes: str | None
    delivery_mode: DeliveryMode
    declared_total_vnd: int
    computed_total_vnd: int
    revision: int
    status: DraftStatus
    consumed_by_document_id: UUID | None


def _draft_row(row: Any) -> DraftRow:
    values = {name: row[name] for name in DraftRow.__dataclass_fields__}
    values["delivery_mode"] = DeliveryMode(values["delivery_mode"])
    return DraftRow(**values)


class DraftRepository(SqlAlchemyRepository):
    def _view(self, row: Any) -> DraftRow | None:
        return _draft_row(row) if row is not None else None

    async def get_by_intent(self, tenant_id: str, intent_id: UUID) -> DraftRow | None:
        table = self.tables.invoice_drafts
        result = await self.session.execute(
            sa.select(table).where(table.c.tenant_id == tenant_id, table.c.intent_id == intent_id)
        )
        return self._view(result.mappings().one_or_none())

    async def get_by_host_ref(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str, for_update: bool = True
    ) -> DraftRow | None:
        table = self.tables.invoice_drafts
        statement = sa.select(table).where(
            table.c.tenant_id == tenant_id,
            table.c.host_ref_type == host_ref_type,
            table.c.host_ref_id == host_ref_id,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        return self._view(result.mappings().one_or_none())

    async def upsert_open(self, draft: dict[str, Any], now: datetime) -> DraftRow:
        table = self.tables.invoice_drafts
        existing = await self.get_by_host_ref(
            draft["tenant_id"], draft["host_ref_type"], draft["host_ref_id"]
        )
        if existing is None:
            values = {"id": uuid4(), **draft}
            result = await self.session.execute(table.insert().values(**values).returning(table))
        else:
            values = draft | {
                "revision": draft.get("revision", existing.revision + 1),
                "updated_at": now,
            }
            result = await self.session.execute(
                table.update().where(table.c.id == existing.id).values(**values).returning(table)
            )
        return _draft_row(result.mappings().one())

    async def mark_consumed(self, draft_id: UUID, document_id: UUID) -> bool:
        table = self.tables.invoice_drafts
        result = await self.session.execute(
            table.update()
            .where(table.c.id == draft_id, table.c.consumed_by_document_id.is_(None))
            .values(status=DraftStatus.CONSUMED.value, consumed_by_document_id=document_id)
        )
        return result.rowcount == 1

    async def reopen(
        self,
        draft_id: UUID,
        intent_id: UUID,
        revision: int,
        now: datetime,
        delivery_mode: DeliveryMode,
    ) -> DraftRow:
        table = self.tables.invoice_drafts
        result = await self.session.execute(
            table.update()
            .where(table.c.id == draft_id)
            .values(
                intent_id=intent_id,
                revision=revision,
                delivery_mode=delivery_mode.value,
                status=DraftStatus.OPEN.value,
                consumed_by_document_id=None,
                updated_at=now,
            )
            .returning(table)
        )
        return _draft_row(result.mappings().one())

    async def discard(self, tenant_id: str, host_ref_type: str, host_ref_id: str) -> bool:
        table = self.tables.invoice_drafts
        result = await self.session.execute(
            table.update()
            .where(
                table.c.tenant_id == tenant_id,
                table.c.host_ref_type == host_ref_type,
                table.c.host_ref_id == host_ref_id,
                table.c.status == DraftStatus.OPEN.value,
            )
            .values(status=DraftStatus.DISCARDED.value, revision=table.c.revision + 1)
        )
        return result.rowcount == 1
