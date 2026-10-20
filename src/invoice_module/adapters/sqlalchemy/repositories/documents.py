import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.domain.document import InvoiceDocument
from invoice_module.domain.enums import DeliveryMode, DocumentStatus, ReviewReason

DEFAULT_POST_LIMIT = 2


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
    delivery_mode: DeliveryMode | None
    approved_at: datetime | None
    approved_by: str | None
    approved_snapshot_sha256: str | None
    approved_reference_code: str | None
    kind: str = "original"
    reference_code: str = ""
    total_vnd: int | None = None
    buyer_source: str | None = None
    request: dict[str, Any] | None = None
    request_frozen_at: datetime | None = None
    request_revision: int = 0
    operator_retry_at: datetime | None = None
    request_authorized_resend: bool = False
    send_started_at: datetime | None = None
    post_count: int = 0
    post_limit: int = DEFAULT_POST_LIMIT
    date_resend_at: datetime | None = None
    queued_at: datetime | None = None
    tracking_code: str | None = None
    provider_invoice_id: str | None = None
    invoice_number: str | None = None
    tax_authority_code: str | None = None
    issued_date: date | None = None
    issued_at: datetime | None = None
    pdf_uri: str | None = None
    xml_uri: str | None = None
    pdf_sha256: str | None = None
    xml_sha256: str | None = None
    attempts: int = 0
    poll_count: int = 0
    lease_owner: str | None = None
    lease_until: datetime | None = None
    lease_generation: int = 0
    next_attempt_at: datetime | None = None
    pending_since: datetime | None = None
    last_error_code: str | None = None
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    cancel_reason: str | None = None
    created_at: datetime | None = None


def _document_row(row: Any) -> DocumentRow:
    values = {name: row[name] for name in DocumentRow.__dataclass_fields__}
    if values["delivery_mode"] is not None:
        values["delivery_mode"] = DeliveryMode(values["delivery_mode"])
    values["status"] = DocumentStatus(values["status"])
    return DocumentRow(**values)


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    """Hash of the canonical form of a frozen snapshot; an approval is bound to this value."""
    canonical = InvoiceDocument.from_json(snapshot).canonical_json()
    return hashlib.sha256(canonical.encode()).hexdigest()


def approval_valid(document: DocumentRow) -> bool:
    """An approval holds only for the exact content and reference that were approved."""
    return (
        document.approved_at is not None
        and document.document is not None
        and document.approved_snapshot_sha256 == snapshot_sha256(document.document)
        and document.approved_reference_code == document.reference_code
    )


def _approval_entry(row: Any, invalidated_by: str, reason: str, now: datetime) -> dict[str, Any]:
    return {
        "approved_at": row["approved_at"].isoformat(),
        "approved_by": row["approved_by"],
        "approved_snapshot_sha256": row["approved_snapshot_sha256"],
        "approved_reference_code": row["approved_reference_code"],
        "invalidated_by": invalidated_by,
        "invalidated_at": now.isoformat(),
        "reason": reason,
    }


class DocumentRepository(SqlAlchemyRepository):
    async def insert(self, row: dict[str, Any]) -> DocumentRow:
        table = self.tables.documents
        result = await self.session.execute(table.insert().values(**row).returning(table))
        return _document_row(result.mappings().one())

    async def get(self, document_id: UUID) -> DocumentRow | None:
        table = self.tables.documents
        result = await self.session.execute(sa.select(table).where(table.c.id == document_id))
        row = result.mappings().one_or_none()
        return _document_row(row) if row is not None else None

    async def lock_for_operator(self, document_id: UUID) -> DocumentRow:
        table = self.tables.documents
        result = await self.session.execute(
            sa.select(table).where(table.c.id == document_id).with_for_update()
        )
        return _document_row(result.mappings().one())

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
        return _document_row(row) if row is not None else None

    async def get_by_host_ref(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str
    ) -> list[DocumentRow]:
        table = self.tables.documents
        result = await self.session.execute(
            sa.select(table)
            .where(
                table.c.tenant_id == tenant_id,
                table.c.host_ref_type == host_ref_type,
                table.c.host_ref_id == host_ref_id,
            )
            .order_by(table.c.created_at, table.c.id)
        )
        return [_document_row(row) for row in result.mappings()]

    async def exists_for_intent(self, tenant_id: str, intent_id: UUID) -> bool:
        table = self.tables.documents
        value = await self.session.scalar(
            sa.select(
                sa.exists().where(table.c.tenant_id == tenant_id, table.c.intent_id == intent_id)
            )
        )
        return bool(value)

    def _open_reason(self, reason: ReviewReason) -> sa.ColumnElement[bool]:
        table, reviews = self.tables.documents, self.tables.review_cases
        return sa.exists(
            sa.select(1).where(
                reviews.c.document_id == table.c.id,
                reviews.c.status == "open",
                sa.cast(reviews.c.details, postgresql.JSONB).contains(
                    {"reasons": [{"reason": reason.value, "status": "open"}]}
                ),
            )
        )

    def _awaiting_draft(self) -> sa.ColumnElement[bool]:
        table = self.tables.documents
        return sa.or_(
            table.c.status == DocumentStatus.NEEDS_DRAFT.value,
            sa.and_(
                table.c.status == DocumentStatus.BLOCKED.value,
                self._open_reason(ReviewReason.AMOUNT_MISMATCH),
            ),
        )

    async def find_awaiting_draft(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str
    ) -> list[DocumentRow]:
        table = self.tables.documents
        result = await self.session.execute(
            sa.select(table)
            .where(
                table.c.tenant_id == tenant_id,
                table.c.host_ref_type == host_ref_type,
                table.c.host_ref_id == host_ref_id,
                self._awaiting_draft(),
            )
            .order_by(table.c.created_at, table.c.id)
        )
        return [_document_row(row) for row in result.mappings()]

    async def list_awaiting_draft(self, limit: int) -> list[DocumentRow]:
        """Every document still waiting for a draft, across hosts, oldest first."""
        table = self.tables.documents
        result = await self.session.execute(
            sa.select(table)
            .where(self._awaiting_draft())
            .order_by(table.c.created_at, table.c.id)
            .limit(limit)
        )
        return [_document_row(row) for row in result.mappings()]

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
        queued_at: datetime | None,
        next_attempt_at: datetime | None,
        draft_id: UUID,
        delivery_mode: DeliveryMode | None,
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
                delivery_mode=delivery_mode.value if delivery_mode is not None else None,
            )
        )

    # Worker claims and compare-and-set writes. Every write made on behalf of a claim is
    # fenced by the lease generation it was claimed with; an operator change bumps it.

    async def claim_due(
        self,
        statuses: Iterable[DocumentStatus],
        now: datetime,
        lease_seconds: int,
        owner: str,
        limit: int,
        count_attempt: bool,
        *,
        extra: sa.ColumnElement[bool] | None = None,
    ) -> list[DocumentRow]:
        table = self.tables.documents
        conditions = [
            table.c.status.in_([status.value for status in statuses]),
            table.c.next_attempt_at.is_not(None),
            table.c.next_attempt_at <= now,
            sa.or_(table.c.lease_until.is_(None), table.c.lease_until <= now),
        ]
        if extra is not None:
            conditions.append(extra)
        due = (
            sa.select(table.c.id)
            .where(*conditions)
            .order_by(table.c.next_attempt_at, table.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("due")
        )
        values: dict[str, Any] = {
            "lease_owner": owner,
            "lease_until": now + timedelta(seconds=lease_seconds),
            "lease_generation": table.c.lease_generation + 1,
            "updated_at": now,
        }
        if count_attempt:
            values["attempts"] = table.c.attempts + 1
        result = await self.session.execute(
            table.update()
            .where(table.c.id.in_(sa.select(due.c.id)))
            .values(**values)
            .returning(table)
        )
        rows = [_document_row(row) for row in result.mappings()]
        return sorted(rows, key=lambda row: (row.next_attempt_at, str(row.id)))

    async def claim_artifacts_due(
        self, now: datetime, lease_seconds: int, owner: str, limit: int
    ) -> list[DocumentRow]:
        table = self.tables.documents
        return await self.claim_due(
            (DocumentStatus.ISSUED,),
            now,
            lease_seconds,
            owner,
            limit,
            count_attempt=True,
            extra=sa.or_(table.c.pdf_uri.is_(None), table.c.xml_uri.is_(None)),
        )

    async def _cas(self, document_id: UUID, lease_generation: int, **values: Any) -> bool:
        table = self.tables.documents
        result = await self.session.execute(
            table.update()
            .where(table.c.id == document_id, table.c.lease_generation == lease_generation)
            .values(**values)
        )
        return result.rowcount == 1

    async def freeze_request(
        self,
        document_id: UUID,
        lease_generation: int,
        request: dict[str, Any],
        issued_date: date,
        now: datetime,
    ) -> bool:
        table = self.tables.documents
        return await self._cas(
            document_id,
            lease_generation,
            request=request,
            request_frozen_at=now,
            issued_date=issued_date,
            request_revision=table.c.request_revision + 1,
            # An operator's permission to resend survives a refreeze for a new date.
            request_authorized_resend=sa.or_(
                table.c.operator_retry_at.is_not(None), table.c.request_authorized_resend
            ),
            operator_retry_at=None,
            updated_at=now,
        )

    async def clear_authorized_resend(self, document_id: UUID, lease_generation: int) -> bool:
        return await self._cas(document_id, lease_generation, request_authorized_resend=False)

    async def update_leased(self, document_id: UUID, lease_generation: int, **values: Any) -> bool:
        """CAS write that keeps the lease, for a step in the middle of a claim."""
        return await self._cas(document_id, lease_generation, **values)

    async def finalize(self, document_id: UUID, lease_generation: int, **values: Any) -> bool:
        """CAS write that records the outcome of a claim and releases its lease."""
        return await self._cas(
            document_id, lease_generation, lease_owner=None, lease_until=None, **values
        )

    async def lease_still_valid(
        self, document_id: UUID, lease_generation: int, until_at_least: datetime
    ) -> bool:
        table = self.tables.documents
        row = (
            await self.session.execute(
                sa.select(table.c.lease_generation, table.c.lease_until).where(
                    table.c.id == document_id
                )
            )
        ).one_or_none()
        return (
            row is not None
            and row.lease_generation == lease_generation
            and row.lease_until is not None
            and row.lease_until >= until_at_least
        )

    async def mark_post(
        self,
        document_id: UUID,
        lease_generation: int,
        now: datetime,
        lease_needed_until: datetime,
        *,
        date_resend: bool = False,
    ) -> bool:
        """The only grant of one transport POST: the lease must outlive the HTTP timeout and the
        reference must have POST budget left. Commit right after, before the transport."""
        table = self.tables.documents
        conditions = [
            table.c.id == document_id,
            table.c.lease_generation == lease_generation,
            table.c.lease_until.is_not(None),
            table.c.lease_until >= lease_needed_until,
            table.c.post_count < table.c.post_limit,
        ]
        values: dict[str, Any] = {
            "send_started_at": sa.func.coalesce(table.c.send_started_at, now),
            "post_count": table.c.post_count + 1,
            "updated_at": now,
        }
        if date_resend:
            conditions += [table.c.post_count == 1, table.c.date_resend_at.is_(None)]
            values["date_resend_at"] = now
        result = await self.session.execute(table.update().where(*conditions).values(**values))
        return result.rowcount == 1

    async def grant_post(self, document_id: UUID) -> None:
        """An operator grants exactly one more POST from the current count, in any state."""
        table = self.tables.documents
        await self.session.execute(
            table.update()
            .where(table.c.id == document_id)
            .values(post_limit=table.c.post_count + 1)
        )

    async def bump_generation(self, document_id: UUID) -> None:
        table = self.tables.documents
        await self.session.execute(
            table.update()
            .where(table.c.id == document_id)
            .values(lease_generation=table.c.lease_generation + 1)
        )

    async def operator_update(self, document_id: UUID, now: datetime, **values: Any) -> None:
        """Write made under ``lock_for_operator``; it fences out any worker claim."""
        table = self.tables.documents
        await self.session.execute(
            table.update()
            .where(table.c.id == document_id)
            .values(
                lease_generation=table.c.lease_generation + 1,
                lease_owner=None,
                lease_until=None,
                updated_at=now,
                **values,
            )
        )

    async def move_to_awaiting_approval(
        self, document_id: UUID, lease_generation: int, now: datetime
    ) -> bool:
        table = self.tables.documents
        result = await self.session.execute(
            table.update()
            .where(
                table.c.id == document_id,
                table.c.lease_generation == lease_generation,
                table.c.status.in_([DocumentStatus.QUEUED.value, DocumentStatus.APPROVED.value]),
            )
            .values(
                status=DocumentStatus.AWAITING_APPROVAL.value,
                next_attempt_at=None,
                lease_owner=None,
                lease_until=None,
                updated_at=now,
            )
        )
        return result.rowcount == 1

    async def approve(
        self,
        document_id: UUID,
        actor: str,
        now: datetime,
        snapshot_sha256: str,
        reference_code: str,
    ) -> bool:
        table = self.tables.documents
        result = await self.session.execute(
            table.update()
            .where(
                table.c.id == document_id,
                table.c.status == DocumentStatus.AWAITING_APPROVAL.value,
            )
            .values(
                status=DocumentStatus.APPROVED.value,
                approved_at=now,
                approved_by=actor,
                approved_snapshot_sha256=snapshot_sha256,
                approved_reference_code=reference_code,
                next_attempt_at=now,
                attempts=0,
                lease_generation=table.c.lease_generation + 1,
                updated_at=now,
            )
        )
        return result.rowcount == 1

    async def record_approval(
        self,
        document_id: UUID,
        actor: str,
        now: datetime,
        snapshot_sha256: str,
        reference_code: str,
    ) -> None:
        """Approval written by an operator who confirms the invoice already exists at the
        provider; the status is set by the caller in the same transaction."""
        table = self.tables.documents
        await self.session.execute(
            table.update()
            .where(table.c.id == document_id)
            .values(
                approved_at=now,
                approved_by=actor,
                approved_snapshot_sha256=snapshot_sha256,
                approved_reference_code=reference_code,
            )
        )

    async def clear_approval(
        self,
        document_id: UUID,
        case_id: UUID | None,
        invalidated_by: str,
        reason: str,
        now: datetime,
        **values: Any,
    ) -> dict[str, Any] | None:
        """Drop an approval and keep it in ``details.previous_approvals`` of the case.

        Without ``case_id`` the open case, or else the latest case, of the document holds the
        audit. ``values`` are written in the same statement (an ``approved`` document must
        leave that status as its approval goes). Returns the audit entry, or ``None`` when
        there was no approval (``values`` are still written).
        """
        table, reviews = self.tables.documents, self.tables.review_cases
        row = (
            (
                await self.session.execute(
                    sa.select(table).where(table.c.id == document_id).with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if row["approved_at"] is None:
            if values:
                await self.session.execute(
                    table.update().where(table.c.id == document_id).values(**values)
                )
            return None
        entry = _approval_entry(row, invalidated_by, reason, now)
        await self.session.execute(
            table.update()
            .where(table.c.id == document_id)
            .values(
                approved_at=None,
                approved_by=None,
                approved_snapshot_sha256=None,
                approved_reference_code=None,
                updated_at=now,
                **values,
            )
        )
        if case_id is None:
            case_id = await self.session.scalar(
                sa.select(reviews.c.id)
                .where(reviews.c.document_id == document_id)
                .order_by(
                    sa.case((reviews.c.status == "open", 0), else_=1),
                    reviews.c.opened_at.desc(),
                )
                .limit(1)
            )
        if case_id is not None:
            details = dict(
                await self.session.scalar(
                    sa.select(reviews.c.details).where(reviews.c.id == case_id).with_for_update()
                )
                or {}
            )
            details["previous_approvals"] = [*details.get("previous_approvals", []), entry]
            await self.session.execute(
                reviews.update().where(reviews.c.id == case_id).values(details=details)
            )
        return entry

    async def set_issuer_account(self, document_id: UUID, issuer_account_id: UUID) -> None:
        table = self.tables.documents
        await self.session.execute(
            table.update()
            .where(table.c.id == document_id)
            .values(issuer_account_id=issuer_account_id)
        )

    async def documents_with_previous_references(
        self, now: datetime, window: timedelta
    ) -> list[tuple[UUID, str, datetime]]:
        """Rotated references still inside the recheck window, skipping documents that wait for
        approval or are held (nothing is sent to the provider for those)."""
        table, reviews = self.tables.documents, self.tables.review_cases
        result = await self.session.execute(
            sa.select(table.c.id, table.c.status, reviews.c.details, reviews.c.reason)
            .join(reviews, reviews.c.document_id == table.c.id)
            .where(
                sa.cast(reviews.c.details, postgresql.JSONB).has_key("previous_references"),
                table.c.status != DocumentStatus.AWAITING_APPROVAL.value,
            )
        )
        held = {
            row.id
            for row in (
                await self.session.execute(
                    sa.select(table.c.id).where(
                        table.c.status == DocumentStatus.BLOCKED.value,
                        self._open_reason(ReviewReason.DRAFT_HELD),
                    )
                )
            )
        }
        found: list[tuple[UUID, str, datetime]] = []
        for row in result:
            if row.id in held:
                continue
            for entry in (row.details or {}).get("previous_references", []):
                rotated_at = datetime.fromisoformat(entry["rotated_at"])
                if rotated_at + window >= now:
                    found.append((row.id, entry["reference_code"], rotated_at))
        return sorted(found, key=lambda item: (item[2], str(item[0])))

    async def list_by_status(
        self, status: DocumentStatus, *, tenant_id: str | None = None, limit: int | None = None
    ) -> list[DocumentRow]:
        table = self.tables.documents
        statement = sa.select(table).where(table.c.status == status.value)
        if tenant_id is not None:
            statement = statement.where(table.c.tenant_id == tenant_id)
        statement = statement.order_by(table.c.settled_at, table.c.id)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return [_document_row(row) for row in result.mappings()]

    async def list_awaiting_approval(
        self, tenant_id: str | None, settled_before: datetime | None, limit: int
    ) -> list[DocumentRow]:
        table = self.tables.documents
        statement = sa.select(table).where(table.c.status == DocumentStatus.AWAITING_APPROVAL.value)
        if tenant_id is not None:
            statement = statement.where(table.c.tenant_id == tenant_id)
        if settled_before is not None:
            statement = statement.where(table.c.settled_at <= settled_before)
        result = await self.session.execute(
            statement.order_by(table.c.settled_at, table.c.id).limit(limit)
        )
        return [_document_row(row) for row in result.mappings()]

    async def claim_timed_out(
        self, now: datetime, pending_timeout: timedelta, limit: int
    ) -> list[DocumentRow]:
        """Lock pending documents past the timeout whose lease is free (no poll in flight)."""
        table = self.tables.documents
        result = await self.session.execute(
            sa.select(table)
            .where(
                table.c.status == DocumentStatus.PENDING_PROVIDER.value,
                table.c.pending_since + pending_timeout < now,
                sa.or_(table.c.lease_until.is_(None), table.c.lease_until <= now),
            )
            .order_by(table.c.pending_since, table.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_document_row(row) for row in result.mappings()]
