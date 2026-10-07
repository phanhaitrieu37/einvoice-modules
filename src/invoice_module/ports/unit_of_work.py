from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from invoice_module.domain.enums import DeliveryMode

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

    async def discard(self, tenant_id: str, host_ref_type: str, host_ref_id: str) -> bool: ...


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


class ReviewCasesRepository(Repository, Protocol):
    async def get_open_id(self, document_id: UUID) -> UUID | None: ...

    async def open_once(
        self, tenant_id: str, document_id: UUID, reason: str, details: dict[str, Any]
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


class IssuerAccountsRepository(Repository, Protocol):
    async def get_active(self, tenant_id: str, merchant_id: UUID, environment: Any) -> Any: ...


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
