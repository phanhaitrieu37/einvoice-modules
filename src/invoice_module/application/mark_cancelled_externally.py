"""Record a cancellation made at the provider portal; the module never cancels through the API."""

import logging
from dataclasses import dataclass
from uuid import UUID

from invoice_module.domain import state
from invoice_module.domain.enums import DocumentStatus
from invoice_module.domain.errors import InvoiceError
from invoice_module.ports.clock import Clock
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)


class CancellationNotAllowed(InvoiceError):
    pass


@dataclass(frozen=True)
class CancellationResult:
    document_id: UUID
    status: DocumentStatus


class MarkCancelledExternally:
    def __init__(self, uow_factory: InvoiceUnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, document_id: UUID, reason: str, actor: str) -> CancellationResult:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            document = await uow.documents.lock_for_operator(document_id)
            if document.status != DocumentStatus.ISSUED:
                raise CancellationNotAllowed(document.status.value)
            state.ensure_transition(document.status, DocumentStatus.CANCELLED_EXTERNALLY)
            await uow.documents.operator_update(
                document_id,
                now,
                status=DocumentStatus.CANCELLED_EXTERNALLY.value,
                cancelled_at=now,
                cancelled_by=actor.strip(),
                cancel_reason=reason,
                next_attempt_at=None,
            )
            await uow.commit()
        logger.info(
            "invoice_cancelled_externally",
            extra={"document_id": str(document_id), "actor": actor.strip()},
        )
        return CancellationResult(document_id, DocumentStatus.CANCELLED_EXTERNALLY)
