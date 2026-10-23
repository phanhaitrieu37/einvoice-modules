"""The single path to ``issued`` and the review helpers every worker path shares."""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow, approval_valid
from invoice_module.domain import state
from invoice_module.domain.enums import (
    DeliveryMode,
    DocumentStatus,
    Environment,
    IssueErrorCode,
    ReviewReason,
    ReviewResolution,
)
from invoice_module.ports.unit_of_work import InvoiceUnitOfWork

logger = logging.getLogger(__name__)

SYSTEM_ACTOR = "system:issuance"

# Reasons a provider outcome opens; a confirmed invoice clears whichever of them is still open.
PROVIDER_REASONS = frozenset(
    {
        ReviewReason.PROVIDER_REJECTED,
        ReviewReason.PROVIDER_FAILED,
        ReviewReason.PROVIDER_TIMEOUT,
        ReviewReason.QUOTA_EXHAUSTED,
        ReviewReason.AUTH_FAILED,
        ReviewReason.MAX_ATTEMPTS_EXCEEDED,
        ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
        ReviewReason.LOOKUP_MISMATCH,
    }
)


class FinalizeResult(StrEnum):
    ISSUED = "issued"
    LOOKUP_MISMATCH = "lookup_mismatch"
    UNAPPROVED = "unapproved"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True)
class IssuedFacts:
    invoice_number: str | None
    tax_authority_code: str | None
    provider_invoice_id: str | None
    provider_issued_date: date | None


def missing_issued_facts(document: DocumentRow, facts: IssuedFacts) -> list[str]:
    missing = []
    if not facts.invoice_number:
        missing.append("invoice_number")
    if facts.provider_issued_date is None:
        missing.append("issued_date")
    if document.environment == Environment.LIVE.value and not facts.tax_authority_code:
        missing.append("tax_authority_code")
    return missing


async def open_review(
    uow: InvoiceUnitOfWork,
    document: DocumentRow,
    reason: ReviewReason,
    details: dict[str, Any],
    now: datetime,
) -> None:
    await uow.review_cases.open_once(document.tenant_id, document.id, reason.value, details, now)


async def fail_document(
    uow: InvoiceUnitOfWork,
    document: DocumentRow,
    lease_generation: int,
    error_code: IssueErrorCode,
    reason: ReviewReason,
    details: dict[str, Any],
    now: datetime,
    **values: Any,
) -> bool:
    """CAS the claimed document to ``failed`` and open the review in the same transaction."""
    state.ensure_transition(document.status, DocumentStatus.FAILED)
    recorded = await uow.documents.finalize(
        document.id,
        lease_generation,
        status=DocumentStatus.FAILED.value,
        last_error_code=error_code.value,
        next_attempt_at=None,
        updated_at=now,
        **values,
    )
    if recorded:
        await open_review(uow, document, reason, details, now)
        # Provider text stays in the database: the log carries codes, states and lengths only.
        message = details.get("provider_message")
        logger.warning(
            "invoice_document_failed",
            extra={
                "document_id": str(document.id),
                "error_code": error_code.value,
                "reason": reason.value,
                "provider_state": details.get("provider_state", details.get("provider_job_state")),
                "provider_message_length": len(message) if isinstance(message, str) else None,
            },
        )
    return recorded


async def finalize_issued(
    uow: InvoiceUnitOfWork,
    document: DocumentRow,
    lease_generation: int,
    *,
    invoice_number: str | None,
    tax_authority_code: str | None,
    provider_invoice_id: str | None,
    provider_issued_date: date | None,
    issued_at: datetime,
    lookup_total_vnd: int | None,
    extra: dict[str, Any] | None = None,
) -> FinalizeResult:
    """Move a document to ``issued`` on provider evidence; the issue date is the provider's.

    ``issued_at`` is when the module recorded the invoice (latency only), never an issue date.
    """
    extra = extra or {}
    if document.delivery_mode == DeliveryMode.MANUAL and not approval_valid(document):
        recorded = await fail_document(
            uow,
            document,
            lease_generation,
            IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
            ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
            {"provider_state": "issued_without_approval", "invoice_number": invoice_number},
            issued_at,
            **extra,
        )
        return FinalizeResult.UNAPPROVED if recorded else FinalizeResult.LEASE_LOST
    if lookup_total_vnd is not None and lookup_total_vnd != document.total_vnd:
        recorded = await fail_document(
            uow,
            document,
            lease_generation,
            IssueErrorCode.LOOKUP_MISMATCH,
            ReviewReason.LOOKUP_MISMATCH,
            {"provider_total": lookup_total_vnd, "document_total": document.total_vnd},
            issued_at,
            **extra,
        )
        return FinalizeResult.LOOKUP_MISMATCH if recorded else FinalizeResult.LEASE_LOST
    facts = IssuedFacts(
        invoice_number, tax_authority_code, provider_invoice_id, provider_issued_date
    )
    missing = missing_issued_facts(document, facts)
    if missing:
        recorded = await fail_document(
            uow,
            document,
            lease_generation,
            IssueErrorCode.LOOKUP_MISMATCH,
            ReviewReason.LOOKUP_MISMATCH,
            {"missing": ",".join(missing)},
            issued_at,
            **extra,
        )
        return FinalizeResult.LOOKUP_MISMATCH if recorded else FinalizeResult.LEASE_LOST
    state.ensure_transition(document.status, DocumentStatus.ISSUED)
    recorded = await uow.documents.finalize(
        document.id,
        lease_generation,
        status=DocumentStatus.ISSUED.value,
        invoice_number=invoice_number,
        tax_authority_code=tax_authority_code,
        provider_invoice_id=provider_invoice_id,
        issued_date=provider_issued_date,
        issued_at=issued_at,
        last_error_code=None,
        attempts=0,
        next_attempt_at=issued_at,
        updated_at=issued_at,
        **extra,
    )
    if not recorded:
        return FinalizeResult.LEASE_LOST
    case_id = await uow.review_cases.get_open_id(document.id)
    if case_id is not None:
        for reason in await uow.review_cases.open_reasons(document.id):
            if ReviewReason(reason) in PROVIDER_REASONS:
                await uow.review_cases.clear_reason(
                    case_id, reason, ReviewResolution.RETRY.value, SYSTEM_ACTOR, None, issued_at
                )
    logger.info(
        "invoice_issued",
        extra={"document_id": str(document.id), "invoice_number": invoice_number},
    )
    return FinalizeResult.ISSUED
