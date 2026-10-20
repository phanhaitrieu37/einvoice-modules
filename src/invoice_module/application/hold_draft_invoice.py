"""Hold a wrong manual draft before anything was sent; it goes to review, never to the provider."""

import logging
from dataclasses import dataclass
from uuid import UUID

from invoice_module.application.issuance import open_review
from invoice_module.domain import state
from invoice_module.domain.enums import DocumentStatus, ReviewReason
from invoice_module.domain.errors import DocumentInFlight, InvoiceError
from invoice_module.ports.clock import Clock
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

_HOLDABLE = (DocumentStatus.AWAITING_APPROVAL, DocumentStatus.APPROVED)


class HoldNotAllowed(InvoiceError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class HoldResult:
    document_id: UUID
    case_id: UUID
    status: DocumentStatus


class HoldDraftInvoice:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        clock: Clock,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._metrics = metrics or NoOpMetricsSink()

    async def execute(self, document_id: UUID, actor: str, note: str) -> HoldResult:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor is required")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("note is required")
        actor = actor.strip()
        now = self._clock.now()
        async with self._uow_factory() as uow:
            document = await uow.documents.lock_for_operator(document_id)
            if document.lease_until is not None and document.lease_until > now:
                raise DocumentInFlight(str(document_id))
            # The durable send mark, not the retry counter, says whether the invoice may have
            # reached the provider; it survives an operator retry.
            if document.send_started_at is not None or document.status in (
                DocumentStatus.PENDING_PROVIDER,
                DocumentStatus.ISSUED,
            ):
                raise HoldNotAllowed("send_started")
            if document.status not in _HOLDABLE:
                raise HoldNotAllowed(document.status.value)
            state.ensure_transition(document.status, DocumentStatus.BLOCKED)
            await open_review(
                uow, document, ReviewReason.DRAFT_HELD, {"note": note, "held_by": actor}, now
            )
            case_id = await uow.review_cases.get_open_id(document_id)
            assert case_id is not None
            await uow.documents.operator_update(document_id, now, next_attempt_at=None)
            await uow.documents.clear_approval(
                document_id, case_id, actor, "held", now, status=DocumentStatus.BLOCKED.value
            )
            await uow.commit()
        logger.info(
            "invoice_draft_held",
            extra={"document_id": str(document_id), "actor": actor, "note": note},
        )
        increment_safely(self._metrics, "invoice_draft_held_total", {})
        return HoldResult(document_id, case_id, DocumentStatus.BLOCKED)
