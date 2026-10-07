from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.enums import ReviewReason, ReviewResolution
from invoice_module.ports.unit_of_work import DuplicateDecision


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

    async def duplicate_decision(self, document_id: UUID) -> DuplicateDecision:
        """The operator's decision on a duplicate settlement, read from every case of the document.

        Only a cleared `retry` releases the hold. A `waive` is final. Any other entry, open or
        cleared by some other resolution, still counts as undecided.
        """
        table = self.tables.review_cases
        cases = await self.session.scalars(
            sa.select(table.c.details).where(table.c.document_id == document_id)
        )
        decisions: set[str] = set()
        for details in cases:
            for item in (details or {}).get("reasons", []):
                if item.get("reason") != ReviewReason.DUPLICATE_SETTLEMENT.value:
                    continue
                resolution = item.get("resolution") if item.get("status") == "cleared" else None
                if resolution == ReviewResolution.WAIVE.value:
                    decisions.add("waived")
                elif resolution == ReviewResolution.RETRY.value:
                    decisions.add("retried")
                else:
                    decisions.add("open")
        if "open" in decisions:
            return "open"
        if "waived" in decisions:
            return "waived"
        if "retried" in decisions:
            return "retried"
        return "none"
