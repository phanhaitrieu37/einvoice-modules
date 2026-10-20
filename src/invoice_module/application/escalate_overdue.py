"""Overdue sweep: there is no provider status webhook, so stuck work is found here.

It re-attaches drafts a wake-up missed, opens ``DRAFT_MISSING`` after an hour, is the only place
that times out a pending invoice, rechecks rotated references inside their window, and raises
alerts (logs and metrics only) for stale work, missing artifacts, overdue reviews and overdue
approvals.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow
from invoice_module.application.attach_draft import (
    attach_draft_to_documents,
    find_draft_for_document,
)
from invoice_module.application.config import InvoiceModuleConfig, business_days_between
from invoice_module.application.issuance import open_review
from invoice_module.domain import state
from invoice_module.domain.enums import (
    DocumentStatus,
    IssueErrorCode,
    ReviewReason,
    ReviewResolution,
)
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.provider import EInvoiceProvider
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

_ALERT_SCAN_LIMIT = 10_000


@dataclass
class EscalationReport:
    drafts_attached: list[UUID] = field(default_factory=list)
    draft_missing_opened: list[UUID] = field(default_factory=list)
    timed_out: list[UUID] = field(default_factory=list)
    stale: list[tuple[UUID, str]] = field(default_factory=list)
    artifacts_missing: list[UUID] = field(default_factory=list)
    reviews_overdue: list[UUID] = field(default_factory=list)
    stale_references: list[tuple[UUID, str]] = field(default_factory=list)
    approvals_overdue: list[UUID] = field(default_factory=list)


class EscalateOverdue:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        provider: EInvoiceProvider | None,
        credentials: CredentialResolver | None,
        clock: Clock,
        metrics: MetricsSink | None = None,
        config: InvoiceModuleConfig | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._credentials = credentials
        self._clock = clock
        self._metrics = metrics or NoOpMetricsSink()
        self._config = config or InvoiceModuleConfig()

    async def run(self, now: datetime | None = None) -> EscalationReport:
        now = now or self._clock.now()
        report = EscalationReport()
        await self._attach_missed_drafts(now, report)
        await self._open_draft_missing(now, report)
        await self._time_out_pending(now, report)
        await self._alert_stale(now, report)
        await self._alert_artifacts_missing(now, report)
        await self._alert_reviews_overdue(now, report)
        await self._recheck_previous_references(now, report)
        await self._alert_approvals_overdue(now, report)
        return report

    async def _attach_missed_drafts(self, now: datetime, report: EscalationReport) -> None:
        async with self._uow_factory() as uow:
            candidates = await uow.documents.list_awaiting_draft(self._config.batch_size)
        for candidate in candidates:
            async with self._uow_factory() as uow:
                await uow.lock_host_ref(
                    candidate.tenant_id, candidate.host_ref_type, candidate.host_ref_id
                )
                current = await uow.documents.lock_for_operator(candidate.id)
                awaiting = await uow.documents.find_awaiting_draft(
                    current.tenant_id, current.host_ref_type, current.host_ref_id
                )
                if current.id not in {document.id for document in awaiting}:
                    continue
                draft = await find_draft_for_document(uow, current)
                if draft is None:
                    continue
                await attach_draft_to_documents(
                    uow, draft, uow.issuer_accounts.get_active, now, documents=[current]
                )
                after = await uow.documents.get(current.id)
                await uow.commit()
            assert after is not None
            if after.document is not None and current.document is None:
                report.drafts_attached.append(current.id)
                increment_safely(self._metrics, "invoice_draft_attached_by_sweep_total", {})

    async def _open_draft_missing(self, now: datetime, report: EscalationReport) -> None:
        async with self._uow_factory() as uow:
            waiting = await uow.documents.list_by_status(
                DocumentStatus.NEEDS_DRAFT, limit=_ALERT_SCAN_LIMIT
            )
            for document in waiting:
                assert document.created_at is not None
                if document.created_at + self._config.draft_missing_after >= now:
                    continue
                if ReviewReason.DRAFT_MISSING.value in await uow.review_cases.open_reasons(
                    document.id
                ) or await uow.review_cases.reason_cleared_with(
                    document.id, ReviewReason.DRAFT_MISSING.value, ReviewResolution.WAIVE.value
                ):
                    continue
                await open_review(
                    uow,
                    document,
                    ReviewReason.DRAFT_MISSING,
                    {"waiting_since": document.created_at.isoformat()},
                    now,
                )
                report.draft_missing_opened.append(document.id)
            await uow.commit()

    async def _time_out_pending(self, now: datetime, report: EscalationReport) -> None:
        async with self._uow_factory() as uow:
            expired = await uow.documents.claim_timed_out(
                now, self._config.pending_timeout, self._config.batch_size
            )
            for document in expired:
                state.ensure_transition(document.status, DocumentStatus.FAILED)
                await uow.documents.operator_update(
                    document.id,
                    now,
                    status=DocumentStatus.FAILED.value,
                    last_error_code=IssueErrorCode.PROVIDER_TIMEOUT.value,
                    next_attempt_at=None,
                )
                await open_review(
                    uow,
                    document,
                    ReviewReason.PROVIDER_TIMEOUT,
                    {
                        "pending_since": document.pending_since.isoformat()
                        if document.pending_since
                        else None,
                        "poll_count": document.poll_count,
                    },
                    now,
                )
                report.timed_out.append(document.id)
            await uow.commit()

    async def _alert_stale(self, now: datetime, report: EscalationReport) -> None:
        limit = self._config.stale_alert_after
        entered = {
            DocumentStatus.QUEUED: lambda row: row.queued_at,
            DocumentStatus.APPROVED: lambda row: row.approved_at,
            DocumentStatus.PENDING_PROVIDER: lambda row: row.pending_since,
        }
        async with self._uow_factory() as uow:
            for status, since in entered.items():
                for document in await uow.documents.list_by_status(status, limit=_ALERT_SCAN_LIMIT):
                    moment = since(document)
                    if moment is None or moment + limit >= now:
                        continue
                    logger.error(
                        "invoice_document_stale",
                        extra={"document_id": str(document.id), "status": status.value},
                    )
                    increment_safely(
                        self._metrics, "invoice_documents_stale_total", {"status": status.value}
                    )
                    report.stale.append((document.id, status.value))

    async def _alert_artifacts_missing(self, now: datetime, report: EscalationReport) -> None:
        async with self._uow_factory() as uow:
            issued = await uow.documents.list_by_status(
                DocumentStatus.ISSUED, limit=_ALERT_SCAN_LIMIT
            )
        for document in issued:
            if document.pdf_uri is not None and document.xml_uri is not None:
                continue
            if document.issued_at is None or (
                document.issued_at + self._config.artifacts_missing_alert_after >= now
            ):
                continue
            increment_safely(self._metrics, "invoice_artifacts_missing_total", {})
            report.artifacts_missing.append(document.id)

    async def _alert_reviews_overdue(self, now: datetime, report: EscalationReport) -> None:
        async with self._uow_factory() as uow:
            cases = await uow.review_cases.list_open(None, _ALERT_SCAN_LIMIT)
        for case in cases:
            if business_days_between(case.opened_at, now) >= (
                self._config.review_overdue_business_days
            ):
                increment_safely(
                    self._metrics, "invoice_review_overdue_total", {"reason": case.reason}
                )
                report.reviews_overdue.append(case.id)

    async def _recheck_previous_references(self, now: datetime, report: EscalationReport) -> None:
        """Detection net only: a rotated reference that turns up means the reconciliation at
        the portal was wrong or the provider received the old request late."""
        if self._provider is None or self._credentials is None:
            return
        async with self._uow_factory() as uow:
            references = await uow.documents.documents_with_previous_references(
                now, self._config.previous_reference_recheck_window
            )
        for document_id, previous_reference, _rotated_at in references:
            async with self._uow_factory() as uow:
                document = await uow.documents.get(document_id)
                known = await uow.review_cases.reason_recorded(
                    document_id,
                    ReviewReason.STALE_REFERENCE_FOUND.value,
                    {"previous_reference": previous_reference},
                )
                account = (
                    await uow.issuer_accounts.get_view(document.issuer_account_id)
                    if document is not None and document.issuer_account_id is not None
                    else None
                )
            if document is None or known or account is None:
                continue
            try:
                creds = await self._credentials.client_credentials(account.credential_ref)
                found = await self._provider.find_by_reference(account, creds, previous_reference)
            except Exception as exc:
                logger.warning(
                    "invoice_previous_reference_recheck_failed",
                    extra={"document_id": str(document_id), "error": type(exc).__name__},
                )
                continue
            if found is None:
                continue
            async with self._uow_factory() as uow:
                await open_review(
                    uow,
                    document,
                    ReviewReason.STALE_REFERENCE_FOUND,
                    {
                        "previous_reference": previous_reference,
                        "provider_state": found.state,
                        "provider_invoice_id": found.provider_invoice_id,
                    },
                    now,
                )
                await uow.commit()
            logger.error(
                "invoice_stale_reference_found",
                extra={"document_id": str(document_id), "previous_reference": previous_reference},
            )
            increment_safely(self._metrics, "invoice_stale_reference_found_total", {})
            report.stale_references.append((document_id, previous_reference))

    async def _alert_approvals_overdue(self, now: datetime, report: EscalationReport) -> None:
        # Alert only: approving is routine accounting work, not a failure for the review queue,
        # and the document keeps its status (no transition).
        async with self._uow_factory() as uow:
            waiting = await uow.documents.list_awaiting_approval(None, None, _ALERT_SCAN_LIMIT)
        for document in waiting:
            if approval_overdue(document, now, self._config):
                logger.error("invoice_approval_overdue", extra={"document_id": str(document.id)})
                increment_safely(self._metrics, "invoice_approval_overdue_total", {})
                report.approvals_overdue.append(document.id)


def approval_overdue(document: DocumentRow, now: datetime, config: InvoiceModuleConfig) -> bool:
    return business_days_between(document.settled_at, now) >= config.approval_overdue_business_days
