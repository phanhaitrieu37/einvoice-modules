"""Poll loop: follow an accepted create until the provider reports a final state.

Polls count in ``poll_count`` and never in ``attempts``; the three-hour timeout is decided only
by the overdue sweep. The issue date recorded is always the provider's, never the poll date.
Only a ``success`` answer reaches ``issued``; a status check that is refused is followed by a
lookup by reference, and an outcome the provider could not confirm goes to review at once.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow
from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.application.issuance import (
    FinalizeResult,
    IssuedFacts,
    fail_document,
    finalize_issued,
    missing_issued_facts,
)
from invoice_module.application.issue_queued import failed_job_details
from invoice_module.domain.enums import DocumentStatus, IssueErrorCode, ReviewReason
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.provider import (
    ClientCredentials,
    EInvoiceProvider,
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderInvoice,
    ProviderProgress,
    ProviderRejected,
    ProviderTransient,
)
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

_TRANSIENT_DELAY = timedelta(seconds=3)
# Set by the adapter on a status check, in any state, when the provider says it could not
# confirm the outcome and asks not to resend: the code is read, never the message.
PROVIDER_UNCONFIRMED = "provider_unconfirmed"


@dataclass(frozen=True)
class PollResult:
    document_id: UUID
    outcome: str


class PollPending:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        provider: EInvoiceProvider,
        credentials: CredentialResolver,
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
        self._owner = self._config.owner()

    async def run_batch(self, limit: int | None = None) -> list[PollResult]:
        async with self._uow_factory() as uow:
            claimed = await uow.documents.claim_due(
                (DocumentStatus.PENDING_PROVIDER,),
                self._clock.now(),
                self._config.lease_seconds,
                self._owner,
                limit or self._config.batch_size,
                count_attempt=False,
            )
            await uow.commit()
        results = []
        for document in claimed:
            try:
                results.append(await self._poll(document))
            except Exception as exc:
                logger.error(
                    "invoice_poll_step_failed",
                    extra={"document_id": str(document.id), "error": type(exc).__name__},
                )
                increment_safely(
                    self._metrics, "invoice_poll_errors_total", {"error": type(exc).__name__}
                )
                results.append(PollResult(document.id, "error"))
        return results

    async def _poll(self, document: DocumentRow) -> PollResult:
        assert document.issuer_account_id is not None
        async with self._uow_factory() as uow:
            account = await uow.issuer_accounts.get_view(document.issuer_account_id)
        assert account is not None
        creds = await self._credentials.client_credentials(account.credential_ref)
        try:
            if document.tracking_code:
                try:
                    progress = await self._provider.check_create(
                        account, creds, document.tracking_code
                    )
                except ProviderRejected as exc:
                    return await self._check_rejected(document, account, creds, exc.code)
                lookup: ProviderInvoice | None = None
            else:
                lookup = await self._provider.find_by_reference(
                    account, creds, document.reference_code
                )
                if lookup is None:
                    return await self._fail(
                        document,
                        IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
                        ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
                        {"provider_state": "none", "phase": "poll"},
                    )
                progress = _progress(lookup)
        except (ProviderTransient, ProviderAuthFailed):
            return await self._reschedule(document, _TRANSIENT_DELAY, count_poll=False)
        if progress.error_code == PROVIDER_UNCONFIRMED:
            return await self._fail(
                document,
                IssueErrorCode.PROVIDER_FAILED,
                ReviewReason.PROVIDER_REJECTED,
                unconfirmed_details(progress),
            )
        return await self._apply(document, account, creds, progress, lookup)

    async def _check_rejected(
        self,
        document: DocumentRow,
        account: IssuerAccountView,
        creds: ClientCredentials,
        check_code: str,
    ) -> PollResult:
        """A refused status check proves nothing about the invoice: the lookup decides."""
        try:
            found = await self._provider.find_by_reference(account, creds, document.reference_code)
        except (ProviderTransient, ProviderAuthFailed):
            return await self._reschedule(document, _TRANSIENT_DELAY, count_poll=False)
        if found is None:
            return await self._fail(
                document,
                IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
                ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
                {"provider_state": "none", "phase": "poll", "check_status": check_code},
            )
        if found.state == "pending":
            # Later polls follow the job by reference.
            now = self._clock.now()
            delay = self._config.next_poll_delay(document.poll_count + 1, None, now)
            return await self._reschedule(document, delay, count_poll=True, tracking_code=None)
        return await self._apply(document, account, creds, _progress(found), found)

    async def _apply(
        self,
        document: DocumentRow,
        account: IssuerAccountView,
        creds: ClientCredentials,
        progress: ProviderProgress,
        lookup: ProviderInvoice | None,
    ) -> PollResult:
        if progress.state == "pending":
            now = self._clock.now()
            delay = self._config.next_poll_delay(
                document.poll_count + 1, progress.next_retry_at, now
            )
            return await self._reschedule(document, delay, count_poll=True)
        if progress.state == "failed":
            return await self._fail(
                document,
                IssueErrorCode.PROVIDER_FAILED,
                ReviewReason.PROVIDER_REJECTED,
                failed_job_details(progress.provider_message),
            )
        if progress.state == "success":
            return await self._success(document, account, creds, progress, lookup)
        # A draft or a cancelled invoice under our reference is never an issued invoice.
        return await self._fail(
            document,
            IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
            ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
            {"provider_state": progress.state},
        )

    async def _success(
        self,
        document: DocumentRow,
        account: IssuerAccountView,
        creds: ClientCredentials,
        progress: ProviderProgress,
        lookup: ProviderInvoice | None,
    ) -> PollResult:
        facts = IssuedFacts(
            progress.invoice_number,
            progress.tax_authority_code,
            progress.provider_invoice_id,
            progress.issued_date,
        )
        if missing_issued_facts(document, facts) and lookup is None:
            # The check answer lacks the provider's number or date: the lookup may carry them.
            try:
                lookup = await self._provider.find_by_reference(
                    account, creds, document.reference_code
                )
            except ProviderTransient:
                return await self._reschedule(document, _TRANSIENT_DELAY, count_poll=False)
            if lookup is not None and lookup.state == "success":
                facts = IssuedFacts(
                    facts.invoice_number or lookup.invoice_number,
                    facts.tax_authority_code or lookup.tax_authority_code,
                    facts.provider_invoice_id or lookup.provider_invoice_id,
                    facts.provider_issued_date or lookup.issued_date,
                )
        missing = missing_issued_facts(document, facts)
        if missing:
            return await self._fail(
                document,
                IssueErrorCode.LOOKUP_MISMATCH,
                ReviewReason.LOOKUP_MISMATCH,
                {"missing": ",".join(missing)},
            )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            result = await finalize_issued(
                uow,
                document,
                document.lease_generation,
                invoice_number=facts.invoice_number,
                tax_authority_code=facts.tax_authority_code,
                provider_invoice_id=facts.provider_invoice_id,
                provider_issued_date=facts.provider_issued_date,
                issued_at=now,
                lookup_total_vnd=lookup.total_amount_vnd if lookup is not None else None,
            )
            if result == FinalizeResult.LEASE_LOST:
                return self._lease_lost(document)
            await uow.commit()
        return PollResult(document.id, result.value)

    async def _reschedule(
        self, document: DocumentRow, delay: timedelta, *, count_poll: bool, **extra: Any
    ) -> PollResult:
        now = self._clock.now()
        values: dict[str, Any] = {"next_attempt_at": now + delay, "updated_at": now, **extra}
        if count_poll:
            values["poll_count"] = document.poll_count + 1
        async with self._uow_factory() as uow:
            if not await uow.documents.finalize(document.id, document.lease_generation, **values):
                return self._lease_lost(document)
            await uow.commit()
        return PollResult(document.id, "pending" if count_poll else "backoff")

    async def _fail(
        self,
        document: DocumentRow,
        error_code: IssueErrorCode,
        reason: ReviewReason,
        details: dict[str, Any],
    ) -> PollResult:
        async with self._uow_factory() as uow:
            if not await fail_document(
                uow,
                document,
                document.lease_generation,
                error_code,
                reason,
                details,
                self._clock.now(),
            ):
                return self._lease_lost(document)
            await uow.commit()
        return PollResult(document.id, DocumentStatus.FAILED.value)

    def _lease_lost(self, document: DocumentRow) -> PollResult:
        logger.warning("invoice_lease_lost", extra={"document_id": str(document.id)})
        increment_safely(self._metrics, "invoice_lease_lost_total", {"loop": "poll"})
        return PollResult(document.id, "lease_lost")


def unconfirmed_details(progress: ProviderProgress) -> dict[str, Any]:
    """Review details of a check whose outcome the provider could not confirm; the message is
    stored verbatim for the operator and never parsed."""
    details: dict[str, Any] = {"unconfirmed": True, "provider_job_state": progress.state}
    if progress.provider_message is not None:
        details["provider_message"] = progress.provider_message
    return details


def _progress(found: ProviderInvoice) -> ProviderProgress:
    return ProviderProgress(
        state=found.state,
        invoice_number=found.invoice_number,
        tax_authority_code=found.tax_authority_code,
        provider_invoice_id=found.provider_invoice_id,
        issued_date=found.issued_date,
        provider_message=found.provider_message,
    )
