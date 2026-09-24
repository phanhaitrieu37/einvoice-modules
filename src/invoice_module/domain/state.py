from invoice_module.domain.enums import DocumentStatus
from invoice_module.domain.errors import InvalidTransition

ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.NEEDS_DRAFT: frozenset({DocumentStatus.QUEUED, DocumentStatus.BLOCKED}),
    DocumentStatus.BLOCKED: frozenset(
        {DocumentStatus.QUEUED, DocumentStatus.NEEDS_DRAFT, DocumentStatus.BLOCKED}
    ),
    DocumentStatus.QUEUED: frozenset(
        {
            DocumentStatus.PENDING_PROVIDER,
            DocumentStatus.QUEUED,
            DocumentStatus.FAILED,
            DocumentStatus.ISSUED,
            DocumentStatus.BLOCKED,
        }
    ),
    DocumentStatus.PENDING_PROVIDER: frozenset(
        {DocumentStatus.PENDING_PROVIDER, DocumentStatus.ISSUED, DocumentStatus.FAILED}
    ),
    DocumentStatus.FAILED: frozenset(
        {DocumentStatus.QUEUED, DocumentStatus.PENDING_PROVIDER, DocumentStatus.ISSUED}
    ),
    DocumentStatus.ISSUED: frozenset(
        {DocumentStatus.REPLACED, DocumentStatus.CANCELLED_EXTERNALLY}
    ),
    DocumentStatus.REPLACED: frozenset(),
    DocumentStatus.CANCELLED_EXTERNALLY: frozenset(),
}


def ensure_transition(current: DocumentStatus, target: DocumentStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(current, target)
