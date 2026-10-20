from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.enums import ReviewReason, ReviewResolution
from invoice_module.ports.unit_of_work import DuplicateDecision


@dataclass(frozen=True)
class ReviewCaseRow:
    id: UUID
    tenant_id: str
    document_id: UUID
    reason: str
    details: dict[str, Any]
    status: str
    resolution: str | None
    resolved_by: str | None
    resolution_note: str | None
    opened_at: datetime
    resolved_at: datetime | None

    def open_reasons(self) -> list[str]:
        return [
            item["reason"]
            for item in self.details.get("reasons", [])
            if item.get("status") == "open"
        ]


def _case_row(row: Any) -> ReviewCaseRow:
    values = {name: row[name] for name in ReviewCaseRow.__dataclass_fields__}
    values["details"] = dict(values["details"] or {})
    return ReviewCaseRow(**values)


class ReviewCaseRepository(SqlAlchemyRepository):
    async def get(self, case_id: UUID, *, for_update: bool = False) -> ReviewCaseRow | None:
        table = self.tables.review_cases
        statement = sa.select(table).where(table.c.id == case_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).mappings().one_or_none()
        return _case_row(row) if row is not None else None

    async def list_for_document(self, document_id: UUID) -> list[ReviewCaseRow]:
        table = self.tables.review_cases
        result = await self.session.execute(
            sa.select(table)
            .where(table.c.document_id == document_id)
            .order_by(table.c.opened_at, table.c.id)
        )
        return [_case_row(row) for row in result.mappings()]

    async def list_open(self, tenant_id: str | None, limit: int) -> list[ReviewCaseRow]:
        table = self.tables.review_cases
        statement = sa.select(table).where(table.c.status == "open")
        if tenant_id is not None:
            statement = statement.where(table.c.tenant_id == tenant_id)
        result = await self.session.execute(
            statement.order_by(table.c.opened_at, table.c.id).limit(limit)
        )
        return [_case_row(row) for row in result.mappings()]

    async def update_details(self, case_id: UUID, details: dict[str, Any]) -> None:
        table = self.tables.review_cases
        await self.session.execute(
            table.update().where(table.c.id == case_id).values(details=details)
        )

    async def append_detail(self, case_id: UUID, key: str, entry: dict[str, Any]) -> None:
        """Append ``entry`` to the list ``details[key]`` of a case (audit trail)."""
        table = self.tables.review_cases
        details = dict(
            await self.session.scalar(
                sa.select(table.c.details).where(table.c.id == case_id).with_for_update()
            )
            or {}
        )
        details[key] = [*details.get(key, []), entry]
        await self.update_details(case_id, details)

    async def reason_cleared_with(self, document_id: UUID, reason: str, resolution: str) -> bool:
        """Whether any case of the document cleared ``reason`` with ``resolution``."""
        return any(
            item.get("reason") == reason
            and item.get("status") == "cleared"
            and item.get("resolution") == resolution
            for case in await self.list_for_document(document_id)
            for item in case.details.get("reasons", [])
        )

    async def reason_recorded(
        self, document_id: UUID, reason: str, details: dict[str, Any]
    ) -> bool:
        """Whether any case of the document, open or closed, carries ``reason`` with details
        containing every key/value of ``details``."""
        return any(
            item.get("reason") == reason
            and all((item.get("details") or {}).get(key) == value for key, value in details.items())
            for case in await self.list_for_document(document_id)
            for item in case.details.get("reasons", [])
        )

    async def get_open_id(self, document_id: UUID) -> UUID | None:
        table = self.tables.review_cases
        return await self.session.scalar(
            sa.select(table.c.id).where(
                table.c.document_id == document_id, table.c.status == "open"
            )
        )

    async def open_once(
        self,
        tenant_id: str,
        document_id: UUID,
        reason: str,
        details: dict[str, Any],
        now: datetime | None = None,
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
            values: dict[str, Any] = {
                "id": case_id,
                "tenant_id": tenant_id,
                "document_id": document_id,
                "reason": reason,
                "details": {"reasons": [{"reason": reason, "details": details, "status": "open"}]},
                "status": "open",
            }
            if now is not None:
                values["opened_at"] = now
            await self.session.execute(table.insert().values(**values))
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
                # The note belongs to the reason it resolves, even while the case stays open.
                if note is not None:
                    item["note"] = note
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
                reason=reason,
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
