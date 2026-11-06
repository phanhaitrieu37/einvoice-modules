"""Read-only queries for hosts, operators and accounting (the CLI builds on these)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow, snapshot_sha256
from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.application.escalate_overdue import approval_overdue
from invoice_module.domain.enums import DeliveryMode, DocumentStatus
from invoice_module.ports.clock import Clock
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

# Statuses in which ``issued_date`` is the provider's issue date. Before the invoice is issued
# the column holds the date frozen into the unsent request, which is not shown to the host.
_ISSUED_BY_PROVIDER = frozenset(
    {DocumentStatus.ISSUED, DocumentStatus.REPLACED, DocumentStatus.CANCELLED_EXTERNALLY}
)


@dataclass(frozen=True)
class DocumentStatusView:
    """One document as a host sees it. ``issued_date`` is the provider's issue date, set only
    once the invoice is ``issued`` (and kept for ``replaced`` and ``cancelled_externally``);
    ``None`` in every other status."""

    document_id: UUID
    settlement_id: UUID
    status: DocumentStatus
    reference_code: str
    invoice_number: str | None
    tax_authority_code: str | None
    issued_date: date | None
    pdf_uri: str | None
    xml_uri: str | None
    open_reasons: tuple[str, ...]


@dataclass(frozen=True)
class OpenReviewView:
    case_id: UUID
    tenant_id: str
    document_id: UUID
    reason: str
    open_reasons: tuple[str, ...]
    opened_at: datetime


@dataclass(frozen=True)
class PendingApprovalView:
    document_id: UUID
    tenant_id: str
    merchant_id: UUID
    settlement_id: UUID
    host_ref_type: str
    host_ref_id: str
    total_vnd: int | None
    buyer_name: str | None
    settled_at: datetime
    age: timedelta


@dataclass(frozen=True)
class DraftForApproval:
    document_id: UUID
    status: DocumentStatus
    delivery_mode: DeliveryMode | None
    reference_code: str
    buyer: dict[str, Any]
    lines: tuple[dict[str, Any], ...]
    total_vnd: int
    notes: str | None
    snapshot_sha256: str


class GetDocumentStatus:
    def __init__(self, uow_factory: InvoiceUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def by_settlement(self, tenant_id: str, settlement_id: UUID) -> DocumentStatusView | None:
        async with self._uow_factory() as uow:
            document = await uow.documents.get_original(tenant_id, settlement_id)
            if document is None:
                return None
            return await self._view(uow, document)

    async def by_host_ref(
        self, tenant_id: str, host_ref_type: str, host_ref_id: str
    ) -> list[DocumentStatusView]:
        async with self._uow_factory() as uow:
            documents = await uow.documents.get_by_host_ref(tenant_id, host_ref_type, host_ref_id)
            return [await self._view(uow, document) for document in documents]

    @staticmethod
    async def _view(uow: Any, document: DocumentRow) -> DocumentStatusView:
        return DocumentStatusView(
            document.id,
            document.settlement_id,
            document.status,
            document.reference_code,
            document.invoice_number,
            document.tax_authority_code,
            document.issued_date if document.status in _ISSUED_BY_PROVIDER else None,
            document.pdf_uri,
            document.xml_uri,
            tuple(await uow.review_cases.open_reasons(document.id)),
        )


class ListOpenReviews:
    def __init__(self, uow_factory: InvoiceUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, tenant_id: str | None = None, limit: int = 100) -> list[OpenReviewView]:
        async with self._uow_factory() as uow:
            cases = await uow.review_cases.list_open(tenant_id, limit)
        return [
            OpenReviewView(
                case.id,
                case.tenant_id,
                case.document_id,
                case.reason,
                tuple(case.open_reasons()),
                case.opened_at,
            )
            for case in cases
        ]


class ListPendingApprovals:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        clock: Clock,
        config: InvoiceModuleConfig | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._config = config or InvoiceModuleConfig()

    async def execute(
        self, tenant_id: str | None = None, overdue_only: bool = False, limit: int = 100
    ) -> list[PendingApprovalView]:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            documents = await uow.documents.list_awaiting_approval(tenant_id, None, limit)
        if overdue_only:
            documents = [
                document for document in documents if approval_overdue(document, now, self._config)
            ]
        return [
            PendingApprovalView(
                document.id,
                document.tenant_id,
                document.merchant_id,
                document.settlement_id,
                document.host_ref_type,
                document.host_ref_id,
                document.total_vnd,
                str((document.document or {}).get("buyer", {}).get("name") or "") or None,
                document.settled_at,
                now - document.settled_at,
            )
            for document in documents
        ]


class GetDraftForApproval:
    def __init__(self, uow_factory: InvoiceUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, document_id: UUID) -> DraftForApproval | None:
        async with self._uow_factory() as uow:
            document = await uow.documents.get(document_id)
        if document is None or document.document is None:
            return None
        snapshot = document.document
        return DraftForApproval(
            document.id,
            document.status,
            document.delivery_mode,
            document.reference_code,
            dict(snapshot["buyer"]),
            tuple(dict(line) for line in snapshot["lines"]),
            int(snapshot["total_vnd"]),
            snapshot.get("notes"),
            snapshot_sha256(snapshot),
        )
