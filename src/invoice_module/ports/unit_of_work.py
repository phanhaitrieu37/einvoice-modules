from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

from invoice_module.domain.enums import DeliveryMode, DocumentStatus

DuplicateDecision = Literal["none", "open", "waived", "retried"]


class Repository(Protocol):
    """Marker protocol; method sets are owned by the application phases."""


class DraftsRepository(Repository, Protocol):
    async def get_by_intent(self, tenant_id: str, intent_id: UUID) -> Any: ...

    async def get_by_host_ref(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str, for_update: bool = True
    ) -> Any: ...

    async def upsert_open(self, draft: dict[str, Any], now: datetime) -> Any: ...

    async def mark_consumed(self, draft_id: UUID, document_id: UUID) -> bool: ...

    async def reopen(
        self,
        draft_id: UUID,
        intent_id: UUID,
        revision: int,
        now: datetime,
        delivery_mode: DeliveryMode,
    ) -> Any: ...

    async def discard(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str, now: datetime | None = None
    ) -> bool: ...


class ReceiptsRepository(Repository, Protocol):
    async def get(self, event_id: UUID) -> dict[str, Any] | None: ...

    async def insert_once(self, receipt: dict[str, Any]) -> bool: ...


class DocumentsRepository(Repository, Protocol):
    async def insert(self, row: dict[str, Any]) -> Any: ...

    async def get_original(self, tenant_id: str, settlement_id: UUID) -> Any: ...

    async def exists_for_intent(self, tenant_id: str, intent_id: UUID) -> bool: ...

    async def find_awaiting_draft(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str
    ) -> list[Any]: ...

    async def has_other_original_for_host_ref(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str, settlement_id: UUID
    ) -> list[UUID]: ...

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
    ) -> None: ...

    async def get(self, document_id: UUID) -> Any: ...

    async def lock_for_operator(self, document_id: UUID) -> Any: ...

    async def claim_due(
        self,
        statuses: Iterable[DocumentStatus],
        now: datetime,
        lease_seconds: int,
        owner: str,
        limit: int,
        count_attempt: bool,
    ) -> list[Any]: ...

    async def claim_artifacts_due(
        self, now: datetime, lease_seconds: int, owner: str, limit: int
    ) -> list[Any]: ...

    async def freeze_request(
        self,
        document_id: UUID,
        lease_generation: int,
        request: dict[str, Any],
        issued_date: date,
        now: datetime,
    ) -> bool: ...

    async def clear_authorized_resend(self, document_id: UUID, lease_generation: int) -> bool: ...

    async def finalize(self, document_id: UUID, lease_generation: int, **values: Any) -> bool: ...

    async def lease_still_valid(
        self, document_id: UUID, lease_generation: int, until_at_least: datetime
    ) -> bool: ...

    async def mark_post(
        self,
        document_id: UUID,
        lease_generation: int,
        now: datetime,
        lease_needed_until: datetime,
        *,
        date_resend: bool = False,
    ) -> bool: ...

    async def grant_post(self, document_id: UUID) -> None: ...

    async def bump_generation(self, document_id: UUID) -> None: ...

    async def operator_update(self, document_id: UUID, now: datetime, **values: Any) -> None: ...

    async def move_to_awaiting_approval(
        self, document_id: UUID, lease_generation: int, now: datetime
    ) -> bool: ...

    async def approve(
        self,
        document_id: UUID,
        actor: str,
        now: datetime,
        snapshot_sha256: str,
        reference_code: str,
    ) -> bool: ...

    async def clear_approval(
        self,
        document_id: UUID,
        case_id: UUID | None,
        invalidated_by: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any] | None: ...

    async def documents_with_previous_references(
        self, now: datetime, window: timedelta
    ) -> list[tuple[UUID, str, datetime]]: ...

    async def list_awaiting_approval(
        self, tenant_id: str | None, settled_before: datetime | None, limit: int
    ) -> list[Any]: ...


class ReviewCasesRepository(Repository, Protocol):
    async def get_open_id(self, document_id: UUID) -> UUID | None: ...

    async def open_once(
        self,
        tenant_id: str,
        document_id: UUID,
        reason: str,
        details: dict[str, Any],
        now: datetime | None = None,
    ) -> UUID: ...

    async def clear_reason(
        self,
        case_id: UUID,
        reason: str,
        resolution: str,
        actor: str,
        note: str | None,
        now: datetime,
    ) -> bool: ...

    async def open_reasons(self, document_id: UUID) -> list[str]: ...

    async def duplicate_decision(self, document_id: UUID) -> DuplicateDecision: ...

    async def get(self, case_id: UUID, *, for_update: bool = False) -> Any: ...

    async def list_open(self, tenant_id: str | None, limit: int) -> list[Any]: ...

    async def append_detail(self, case_id: UUID, key: str, entry: dict[str, Any]) -> None: ...

    async def reason_cleared_with(
        self, document_id: UUID, reason: str, resolution: str
    ) -> bool: ...


class IssuerAccountsRepository(Repository, Protocol):
    async def get_active(self, tenant_id: str, merchant_id: UUID, environment: Any) -> Any: ...

    async def get_view(self, account_id: UUID) -> Any: ...


class InvoiceUnitOfWork(Protocol):
    issuer_accounts: IssuerAccountsRepository
    drafts: DraftsRepository
    receipts: ReceiptsRepository
    documents: DocumentsRepository
    review_cases: ReviewCasesRepository

    async def lock_host_ref(self, tenant_id: str, host_ref_type: str, host_ref_id: str) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def __aenter__(self) -> "InvoiceUnitOfWork": ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class InvoiceUnitOfWorkFactory(Protocol):
    def __call__(self) -> InvoiceUnitOfWork: ...
