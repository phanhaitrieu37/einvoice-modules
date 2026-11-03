"""Accounting approval of a manual draft kept in the module (nothing exists at the provider yet).

An approval is bound to the snapshot hash and the reference code it was given for; the worker
creates and issues the invoice in one step only while both still match.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from invoice_module.adapters.sqlalchemy.repositories.documents import snapshot_sha256
from invoice_module.domain import state
from invoice_module.domain.enums import DocumentStatus
from invoice_module.domain.errors import DocumentInFlight, InvoiceError
from invoice_module.ports.clock import Clock
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)


class ApprovalNotAllowed(InvoiceError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ApprovalResult:
    document_id: UUID
    status: DocumentStatus
    already_approved: bool


class ApproveDraftInvoice:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        clock: Clock,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._metrics = metrics or NoOpMetricsSink()

    async def execute(
        self,
        document_id: UUID,
        actor: str,
        note: str | None = None,
        *,
        expected_snapshot_sha256: str | None = None,
    ) -> ApprovalResult:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor is required")
        actor = actor.strip()
        now = self._clock.now()
        async with self._uow_factory() as uow:
            document = await uow.documents.lock_for_operator(document_id)
            if document.lease_until is not None and document.lease_until > now:
                raise DocumentInFlight(str(document_id))
            if document.status != DocumentStatus.AWAITING_APPROVAL:
                if document.approved_at is not None:
                    return ApprovalResult(document_id, document.status, already_approved=True)
                raise ApprovalNotAllowed(document.status.value)
            assert document.document is not None
            digest = snapshot_sha256(document.document)
            if expected_snapshot_sha256 is not None and expected_snapshot_sha256 != digest:
                raise ApprovalNotAllowed("snapshot_changed")
            state.ensure_transition(document.status, DocumentStatus.APPROVED)
            if not await uow.documents.approve(
                document_id, actor, now, digest, document.reference_code
            ):
                raise ApprovalNotAllowed(document.status.value)
            await uow.commit()
        logger.info(
            "invoice_draft_approved",
            # The note is free operator text and may name the buyer: only its length is logged.
            extra={"document_id": str(document_id), "actor": actor, "note_length": len(note or "")},
        )
        increment_safely(self._metrics, "invoice_draft_approved_total", {})
        return ApprovalResult(document_id, DocumentStatus.APPROVED, already_approved=False)
