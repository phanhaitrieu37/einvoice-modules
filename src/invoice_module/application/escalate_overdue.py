"""Overdue sweep: there is no provider status webhook, so stuck work is found here.

It re-attaches drafts a wake-up missed, opens ``DRAFT_MISSING`` after an hour, is the only place
that times out a pending invoice, rechecks rotated references inside their window, and raises
alerts (logs and metrics only) for stale work, missing artifacts, overdue reviews and overdue
approvals.
"""

import logging
from collections.abc import Awaitable, Callable
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
from invoice_module.domain.enums import DocumentStatus, IssueErrorCode, ReviewReason
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.provider import EInvoiceProvider
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

_ALERT_SCAN_LIMIT = 10_000
# The column that marks when a document entered a status the stale alert watches.
_ENTERED = {
    DocumentStatus.QUEUED: "queued_at",
    DocumentStatus.APPROVED: "approved_at",
    DocumentStatus.PENDING_PROVIDER: "pending_since",
}


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
    errors: list[tuple[str, str]] = field(default_factory=list)


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
        # Process memory only: what a restart forgets is at most one extra log line or lookup.
        # A stale or overdue entry logs once while it stays listed; the metric counts every
        # sweep. An entry that left the list is forgotten, so a later occurrence logs again.
        self._rechecked_at: dict[tuple[UUID, str], datetime] = {}
        self._stale_logged: set[tuple[UUID, str, datetime]] = set()
        self._overdue_logged: set[tuple[UUID, datetime]] = set()

    async def run(self, now: datetime | None = None) -> EscalationReport:
        """Run every sub-sweep; one that fails is logged and counted, and the rest still run."""
        now = now or self._clock.now()
        report = EscalationReport()
        steps: list[tuple[str, Callable[[datetime, EscalationReport], Awaitable[None]]]] = [
            ("attach_drafts", self._attach_missed_drafts),
            ("draft_missing", self._open_draft_missing),
            ("pending_timeout", self._time_out_pending),
            ("stale", self._alert_stale),
            ("artifacts_missing", self._alert_artifacts_missing),
            ("reviews_overdue", self._alert_reviews_overdue),
            ("previous_references", self._recheck_previous_references),
            ("approvals_overdue", self._alert_approvals_overdue),
        ]
        for name, step in steps:
            try:
                await step(now, report)
            except Exception as exc:
                self._step_failed(report, name, exc)
        return report

    def _step_failed(
        self, report: EscalationReport, step: str, exc: Exception, **extra: str
    ) -> None:
        logger.error(
            "invoice_sweep_step_failed",
            extra={"step": step, "error": type(exc).__name__, **extra},
        )
        increment_safely(self._metrics, "invoice_sweep_step_errors_total", {"step": step})
        report.errors.append((step, type(exc).__name__))

    async def _attach_missed_drafts(self, now: datetime, report: EscalationReport) -> None:
        async with self._uow_factory() as uow:
            candidates = await uow.documents.list_awaiting_draft(self._config.batch_size)
        for candidate in candidates:
            try:
                await self._attach_missed_draft(candidate, now, report)
            except Exception as exc:
                # One document that cannot attach must not hold back the others.
                self._step_failed(report, "attach_draft", exc, document_id=str(candidate.id))

    async def _attach_missed_draft(
        self, candidate: DocumentRow, now: datetime, report: EscalationReport
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.lock_host_ref(
                candidate.tenant_id, candidate.host_ref_type, candidate.host_ref_id
            )
            current = await uow.documents.lock_for_operator(candidate.id)
            awaiting = await uow.documents.find_awaiting_draft(
                current.tenant_id, current.host_ref_type, current.host_ref_id
            )
            if current.id not in {document.id for document in awaiting}:
                return
            draft = await find_draft_for_document(uow, current)
            if draft is None:
                return
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
        # The query leaves out documents whose DRAFT_MISSING is open or was waived: the sweep
        # never reopens what an operator settled.
        async with self._uow_factory() as uow:
            waiting = await uow.documents.list_draft_missing_due(
                now - self._config.draft_missing_after, _ALERT_SCAN_LIMIT
            )
            for document in waiting:
                assert document.created_at is not None
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
                        # ``transient_error`` when the last poll got no usable answer.
                        "last_poll_error": document.last_error_code,
                    },
                    now,
                )
                report.timed_out.append(document.id)
            await uow.commit()

    async def _alert_stale(self, now: datetime, report: EscalationReport) -> None:
        # The threshold is in the query, so stale documents are found however many fresh ones
        # sort before them.
        before = now - self._config.stale_alert_after
        seen: set[tuple[UUID, str, datetime]] = set()
        async with self._uow_factory() as uow:
            for status, entered in _ENTERED.items():
                for document in await uow.documents.list_entered_before(
                    status, entered, before, _ALERT_SCAN_LIMIT
                ):
                    key = (document.id, status.value, getattr(document, entered))
                    seen.add(key)
                    if key not in self._stale_logged:
                        logger.error(
                            "invoice_document_stale",
                            extra={"document_id": str(document.id), "status": status.value},
                        )
                    increment_safely(
                        self._metrics, "invoice_documents_stale_total", {"status": status.value}
                    )
                    report.stale.append((document.id, status.value))
        self._stale_logged = seen

    async def _alert_artifacts_missing(self, now: datetime, report: EscalationReport) -> None:
        async with self._uow_factory() as uow:
            issued = await uow.documents.list_missing_artifacts(
                now - self._config.artifacts_missing_alert_after, _ALERT_SCAN_LIMIT
            )
        for document in issued:
            increment_safely(self._metrics, "invoice_artifacts_missing_total", {})
            report.artifacts_missing.append(document.id)

    async def _alert_reviews_overdue(self, now: datetime, report: EscalationReport) -> None:
        # Oldest first: a truncated list drops only the newest cases, which cannot be overdue
        # before the older ones (business days are not worth computing in SQL).
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
        interval = self._config.previous_reference_recheck_interval
        listed = {(document_id, reference) for document_id, reference, _ in references}
        self._rechecked_at = {
            key: moment for key, moment in self._rechecked_at.items() if key in listed
        }
        for document_id, previous_reference, _rotated_at in references:
            last = self._rechecked_at.get((document_id, previous_reference))
            if last is not None and now - last < interval:
                continue
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
            self._rechecked_at[(document_id, previous_reference)] = now
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
        # and the document keeps its status (no transition). Ordered by settlement, oldest
        # first: a truncated list drops only documents that cannot be overdue before the rest.
        async with self._uow_factory() as uow:
            waiting = await uow.documents.list_awaiting_approval(None, None, _ALERT_SCAN_LIMIT)
        seen: set[tuple[UUID, datetime]] = set()
        for document in waiting:
            if approval_overdue(document, now, self._config):
                key = (document.id, document.settled_at)
                seen.add(key)
                if key not in self._overdue_logged:
                    logger.error(
                        "invoice_approval_overdue", extra={"document_id": str(document.id)}
                    )
                increment_safely(self._metrics, "invoice_approval_overdue_total", {})
                report.approvals_overdue.append(document.id)
        self._overdue_logged = seen


def approval_overdue(document: DocumentRow, now: datetime, config: InvoiceModuleConfig) -> bool:
    return business_days_between(document.settled_at, now) >= config.approval_overdue_business_days
