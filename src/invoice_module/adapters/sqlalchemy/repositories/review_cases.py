from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository


class ReviewCaseRepository(SqlAlchemyRepository):
    async def get_open_id(self, document_id: UUID) -> UUID | None:
        table = self.tables.review_cases
        return await self.session.scalar(
            sa.select(table.c.id).where(
                table.c.document_id == document_id, table.c.status == "open"
            )
        )

    async def open_once(
        self, tenant_id: str, document_id: UUID, reason: str, details: dict[str, Any]
    ) -> UUID:
        table = self.tables.review_cases
        result = await self.session.execute(
            sa.select(table)
            .where(table.c.document_id == document_id, table.c.status == "open")
            .with_for_update()
        )
        existing = result.mappings().one_or_none()
        if existing is None:
            case_id = uuid4()
            await self.session.execute(
                table.insert().values(
                    id=case_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    reason=reason,
                    details={"reasons": [{"reason": reason, "details": details, "status": "open"}]},
                    status="open",
                )
            )
            return case_id
        value = dict(existing["details"] or {})
        reasons = list(value.get("reasons", []))
        if not any(
            item.get("reason") == reason and item.get("status") == "open" for item in reasons
        ):
            reasons.append({"reason": reason, "details": details, "status": "open"})
            value["reasons"] = reasons
            await self.session.execute(
                table.update().where(table.c.id == existing["id"]).values(details=value)
            )
        return existing["id"]

    async def clear_reason(
        self,
        case_id: UUID,
        reason: str,
        resolution: str,
        actor: str,
        note: str | None,
        now: datetime,
    ) -> bool:
        table = self.tables.review_cases
        result = await self.session.execute(
            sa.select(table)
            .where(table.c.id == case_id, table.c.status == "open")
            .with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None:
            return False
        details = dict(row["details"])
        reasons = list(details.get("reasons", []))
        matched = False
        for item in reasons:
            if item.get("reason") == reason and item.get("status") == "open":
                item.update(
                    status="cleared",
                    cleared_by=actor,
                    cleared_at=now.isoformat(),
                    resolution=resolution,
                )
                matched = True
                break
        if not matched:
            return False
        open_items = [item for item in reasons if item.get("status") == "open"]
        details["reasons"] = reasons
        values: dict[str, Any] = {"details": details}
        if open_items:
            values["reason"] = open_items[0]["reason"]
        else:
            values.update(
                status="resolved",
                resolution=resolution,
                resolved_by=actor,
                resolution_note=note,
                resolved_at=now,
            )
        await self.session.execute(table.update().where(table.c.id == case_id).values(**values))
        return not open_items

    async def open_reasons(self, document_id: UUID) -> list[str]:
        table = self.tables.review_cases
        result = await self.session.execute(
            sa.select(table.c.details).where(
                table.c.document_id == document_id, table.c.status == "open"
            )
        )
        row = result.scalar_one_or_none()
        return [
            item["reason"]
            for item in (row or {}).get("reasons", [])
            if item.get("status") == "open"
        ]
