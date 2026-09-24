from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository


class ReceiptRepository(SqlAlchemyRepository):
    async def get(self, event_id: UUID) -> dict[str, Any] | None:
        table = self.tables.settlement_receipts
        result = await self.session.execute(table.select().where(table.c.event_id == event_id))
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else None

    async def insert_once(self, receipt: dict[str, Any]) -> bool:
        table = self.tables.settlement_receipts
        dialect = self.session.bind.dialect.name
        statement = (sqlite_insert if dialect == "sqlite" else pg_insert)(table).values(**receipt)
        statement = statement.on_conflict_do_nothing(index_elements=[table.c.event_id]).returning(
            table.c.event_id
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None
