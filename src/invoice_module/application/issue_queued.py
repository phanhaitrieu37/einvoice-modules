"""Issue loop: freeze the create request just before sending, send it once per POST grant, and
record the outcome with a compare-and-set on the claim's lease generation.

Every POST goes through the send-boundary hook, where ``mark_post`` is the only grant of one
transport request; the reference has a POST budget (two automatic POSTs by default, one more
per operator grant), so no path can send a third automatic create for a reference. Any POST
whose outcome is unknown is followed by a lookup before anything else is sent. The reference
code never changes here.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow, approval_valid
from invoice_module.application.config import InvoiceModuleConfig, retry_delay, today_vn
from invoice_module.application.issuance import (
    FinalizeResult,
    fail_document,
    finalize_issued,
    open_review,
)
from invoice_module.domain import state
from invoice_module.domain.document import InvoiceDocument
from invoice_module.domain.enums import (
    CrossDayAction,
    DeliveryMode,
    DocumentStatus,
    IssueErrorCode,
    ReviewReason,
    ReviewResolution,
)
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.provider import (
    Accepted,
    ClientCredentials,
    DocumentExists,
    EInvoiceProvider,
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderInvoice,
    ProviderRejected,
    ProviderTransient,
    QuotaExhausted,
    SendAborted,
)
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

ISSUED_DATE_INVALID = IssueErrorCode.ISSUED_DATE_INVALID.value
_REJECTION_CODES = {
    IssueErrorCode.VALIDATION_ERROR.value,
    IssueErrorCode.ISSUED_DATE_INVALID.value,
    IssueErrorCode.REGISTRATION_NOT_APPROVED.value,
    IssueErrorCode.SELLER_STORE_INVALID.value,
}


@dataclass(frozen=True)
class IssueResult:
    document_id: UUID
    outcome: str


@dataclass
class _SendGuard:
    reason: str | None = None
    granted: int = 0


class IssueQueued:
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

    async def run_batch(self, limit: int | None = None) -> list[IssueResult]:
        async with self._uow_factory() as uow:
            claimed = await uow.documents.claim_due(
                (DocumentStatus.QUEUED, DocumentStatus.APPROVED),
                self._clock.now(),
                self._config.lease_seconds,
                self._owner,
                limit or self._config.batch_size,
                count_attempt=True,
            )
            await uow.commit()
        results = []
        for document in claimed:
            try:
                results.append(await self._process(document))
            except Exception as exc:
                # The lease expires and a later claim looks the reference up before any send.
                logger.exception(
                    "invoice_issue_step_failed",
                    extra={"document_id": str(document.id), "error": type(exc).__name__},
                )
                self._count("invoice_issue_errors_total", error=type(exc).__name__)
                results.append(IssueResult(document.id, "error"))
        return results

    # -- claim routing ---------------------------------------------------------------------

    async def _process(self, document: DocumentRow) -> IssueResult:
        if document.attempts > self._config.issue_max_attempts:
            return await self._fail(
                document,
                IssueErrorCode.MAX_ATTEMPTS_EXCEEDED,
                ReviewReason.MAX_ATTEMPTS_EXCEEDED,
                {
                    "attempts": document.attempts,
                    "issue_max_attempts": self._config.issue_max_attempts,
                },
            )
        account = await self._account(document)
        if (
            document.status == DocumentStatus.QUEUED
            and document.delivery_mode == DeliveryMode.MANUAL
        ):
            return await self._await_approval(document, account)
        if document.status == DocumentStatus.APPROVED and not approval_valid(document):
            return await self._reset_stale_approval(document, account)
        if document.request is None:
            return await self._freeze_and_send(document, account)
        return await self._lookup_then_send(document, account)

    async def _await_approval(
        self, document: DocumentRow, account: IssuerAccountView
    ) -> IssueResult:
        """An unapproved manual draft stays in the module: no provider call before approval."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            if document.approved_at is not None:
                await uow.documents.clear_approval(
                    document.id, None, "system:issue", "returned_to_queue", now
                )
            if document.request is None:
                issued_date = today_vn(now)
                request = self._build(account, document, issued_date)
                if not await uow.documents.freeze_request(
                    document.id, document.lease_generation, request, issued_date, now
                ):
                    return self._lease_lost(document)
            state.ensure_transition(document.status, DocumentStatus.AWAITING_APPROVAL)
            if not await uow.documents.move_to_awaiting_approval(
                document.id, document.lease_generation, now
            ):
                return self._lease_lost(document)
            await uow.commit()
        return IssueResult(document.id, DocumentStatus.AWAITING_APPROVAL.value)

    async def _reset_stale_approval(
        self, document: DocumentRow, account: IssuerAccountView
    ) -> IssueResult:
        """The approved content changed: drop the approval and rebuild the request from the
        current snapshot, so the earlier content can never be sent."""
        now = self._clock.now()
        issued_date = today_vn(now)
        request = self._build(account, document, issued_date)
        async with self._uow_factory() as uow:
            # The freeze is the CAS and locks the row; the approval goes in the same statement
            # that leaves ``approved``.
            if not await uow.documents.freeze_request(
                document.id, document.lease_generation, request, issued_date, now
            ):
                return self._lease_lost(document)
            state.ensure_transition(document.status, DocumentStatus.AWAITING_APPROVAL)
            await uow.documents.clear_approval(
                document.id,
                None,
                "system:issue",
                "snapshot_changed",
                now,
                status=DocumentStatus.AWAITING_APPROVAL.value,
                next_attempt_at=None,
                lease_owner=None,
                lease_until=None,
            )
            await uow.commit()
        logger.warning("invoice_approval_invalidated", extra={"document_id": str(document.id)})
        return IssueResult(document.id, DocumentStatus.AWAITING_APPROVAL.value)

    async def _freeze_and_send(
        self, document: DocumentRow, account: IssuerAccountView
    ) -> IssueResult:
        now = self._clock.now()
        issued_date = today_vn(now)
        if document.delivery_mode == DeliveryMode.AUTO and issued_date != today_vn(
            document.settled_at
        ):
            blocked = await self._cross_day(document, account, issued_date)
            if blocked is not None:
                return blocked
        return await self._freeze_send(document, account, issued_date)

    async def _lookup_then_send(
        self, document: DocumentRow, account: IssuerAccountView
    ) -> IssueResult:
        """A frozen request means an earlier send may have reached the provider: look first."""
        creds = await self._creds(account)
        try:
            found = await self._provider.find_by_reference(account, creds, document.reference_code)
        except ProviderTransient as exc:
            return await self._backoff(document, exc.retry_after)
        except ProviderAuthFailed:
            return await self._fail(
                document, IssueErrorCode.AUTH_FAILED, ReviewReason.AUTH_FAILED, {"phase": "lookup"}
            )
        lookup_state = "none"
        if found is not None:
            if found.state != "failed" or not document.request_authorized_resend:
                return await self._apply_lookup(document, found)
            lookup_state = "failed"  # an operator allowed a resend after this known failure
        if document.post_count >= document.post_limit:
            return await self._fail(
                document,
                IssueErrorCode.MAX_ATTEMPTS_EXCEEDED,
                ReviewReason.MAX_ATTEMPTS_EXCEEDED,
                {
                    "post_count": document.post_count,
                    "post_limit": document.post_limit,
                    "lookup_state": lookup_state,
                    "date_resend_used": document.date_resend_at is not None,
                },
            )
        today = today_vn(self._clock.now())
        if today == document.issued_date:
            assert document.request is not None
            return await self._send(document, account, document.request, today)
        if document.delivery_mode == DeliveryMode.AUTO:
            blocked = await self._cross_day(document, account, today)
            if blocked is not None:
                return blocked
        return await self._freeze_send(document, account, today)

    async def _cross_day(
        self, document: DocumentRow, account: IssuerAccountView, issued_date: date
    ) -> IssueResult | None:
        """The issue date would differ from the settlement date (or from the frozen request)."""
        action = account.issue_date_cross_day_action
        if action != CrossDayAction.ISSUE and not await self._cross_day_confirmed(document):
            now = self._clock.now()
            async with self._uow_factory() as uow:
                state.ensure_transition(document.status, DocumentStatus.BLOCKED)
                if not await uow.documents.finalize(
                    document.id,
                    document.lease_generation,
                    status=DocumentStatus.BLOCKED.value,
                    next_attempt_at=None,
                    updated_at=now,
                ):
                    return self._lease_lost(document)
                await open_review(
                    uow,
                    document,
                    ReviewReason.ISSUE_DATE_CROSSED_DAY,
                    {
                        "settled_date": today_vn(document.settled_at).isoformat(),
                        "frozen_issued_date": (
                            document.issued_date.isoformat() if document.issued_date else None
                        ),
                        "issue_date": issued_date.isoformat(),
                    },
                    now,
                )
                await uow.commit()
            return IssueResult(document.id, DocumentStatus.BLOCKED.value)
        self._count("invoice_cross_day_total")
        return None

    async def _cross_day_confirmed(self, document: DocumentRow) -> bool:
        async with self._uow_factory() as uow:
            return await uow.review_cases.reason_cleared_with(
                document.id,
                ReviewReason.ISSUE_DATE_CROSSED_DAY.value,
                ReviewResolution.RETRY.value,
            )

    async def _freeze_send(
        self,
        document: DocumentRow,
        account: IssuerAccountView,
        issued_date: date,
        *,
        date_resend: bool = False,
    ) -> IssueResult:
        now = self._clock.now()
        request = self._build(account, document, issued_date)
        async with self._uow_factory() as uow:
            if date_resend:
                # The 400 answered the earlier POST: that permission to resend is spent.
                await uow.documents.clear_authorized_resend(document.id, document.lease_generation)
            if not await uow.documents.freeze_request(
                document.id, document.lease_generation, request, issued_date, now
            ):
                return self._lease_lost(document)
            await uow.commit()
        return await self._send(document, account, request, issued_date, date_resend=date_resend)

    # -- the send --------------------------------------------------------------------------

    async def _send(
        self,
        document: DocumentRow,
        account: IssuerAccountView,
        request: dict[str, Any],
        issued_date: date,
        *,
        date_resend: bool = False,
    ) -> IssueResult:
        creds = await self._creds(account)
        guard = _SendGuard()

        async def before_send() -> bool:
            now = self._clock.now()
            if today_vn(now) != issued_date:
                guard.reason = "date_rolled"
                return False
            async with self._uow_factory() as uow:
                granted = await uow.documents.mark_post(
                    document.id,
                    document.lease_generation,
                    now,
                    now + timedelta(seconds=self._config.http_timeout_s),
                    date_resend=date_resend,
                )
                await uow.commit()
            if not granted:
                guard.reason = await self._skip_reason(document, now)
                return False
            guard.granted += 1
            return True

        try:
            accepted = await self._provider.create(account, creds, request, before_send=before_send)
        except SendAborted:
            return await self._aborted(document, guard.reason or "lease")
        except DocumentExists:
            if date_resend:
                return await self._date_resend_conflict(document, account, creds)
            return await self._conflict(document, account, creds)
        except ProviderTransient as exc:
            return await self._transient(document, exc.retry_after)
        except QuotaExhausted:
            return await self._quota(document)
        except ProviderRejected as exc:
            if exc.code == ISSUED_DATE_INVALID:
                if date_resend:
                    return await self._fail(
                        document,
                        IssueErrorCode.ISSUED_DATE_INVALID,
                        ReviewReason.PROVIDER_REJECTED,
                        {
                            "code": exc.code,
                            "after_date_resend": True,
                            "post_count": document.post_count + 1,
                        },
                        request_authorized_resend=False,
                    )
                return await self._date_rejected(document, account, creds)
            return await self._rejected(document, exc.code)
        except ProviderAuthFailed:
            return await self._fail(
                document,
                IssueErrorCode.AUTH_FAILED,
                ReviewReason.AUTH_FAILED,
                {"phase": "create", "post_granted": guard.granted > 0},
                request_authorized_resend=False,
            )
        return await self._accepted(document, accepted)

    async def _skip_reason(self, document: DocumentRow, now: datetime) -> str:
        async with self._uow_factory() as uow:
            lease_ok = await uow.documents.lease_still_valid(
                document.id,
                document.lease_generation,
                now + timedelta(seconds=self._config.http_timeout_s),
            )
        return "budget" if lease_ok else "lease"

    # -- outcomes --------------------------------------------------------------------------

    async def _accepted(self, document: DocumentRow, accepted: Accepted) -> IssueResult:
        return await self._to_pending(document, accepted.tracking_code)

    async def _to_pending(self, document: DocumentRow, tracking_code: str | None) -> IssueResult:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            state.ensure_transition(document.status, DocumentStatus.PENDING_PROVIDER)
            if not await uow.documents.finalize(
                document.id,
                document.lease_generation,
                status=DocumentStatus.PENDING_PROVIDER.value,
                tracking_code=tracking_code,
                pending_since=now,
                poll_count=0,
                next_attempt_at=now + self._config.next_poll_delay(0, None, now),
                request_authorized_resend=False,
                last_error_code=None,
                updated_at=now,
            ):
                return self._lease_lost(document)
            await uow.commit()
        return IssueResult(document.id, DocumentStatus.PENDING_PROVIDER.value)

    async def _apply_lookup(self, document: DocumentRow, found: ProviderInvoice) -> IssueResult:
        if found.state == "success":
            return await self._finalize_from_lookup(document, found)
        if found.state == "pending":
            return await self._to_pending(document, found.tracking_code)
        if found.state == "draft":
            return await self._fail(
                document,
                IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
                ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
                {"provider_state": "draft"},
                request_authorized_resend=False,
            )
        return await self._fail(
            document,
            IssueErrorCode.PROVIDER_FAILED,
            ReviewReason.PROVIDER_REJECTED,
            failed_job_details(found.provider_message),
        )

    async def _finalize_from_lookup(
        self, document: DocumentRow, found: ProviderInvoice
    ) -> IssueResult:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            result = await finalize_issued(
                uow,
                document,
                document.lease_generation,
                invoice_number=found.invoice_number,
                tax_authority_code=found.tax_authority_code,
                provider_invoice_id=found.provider_invoice_id,
                provider_issued_date=found.issued_date,
                issued_at=now,
                lookup_total_vnd=found.total_amount_vnd,
                extra={"request_authorized_resend": False},
            )
            if result == FinalizeResult.LEASE_LOST:
                return self._lease_lost(document)
            await uow.commit()
        return IssueResult(
            document.id,
            DocumentStatus.ISSUED.value if result == FinalizeResult.ISSUED else "failed",
        )

    async def _conflict(
        self, document: DocumentRow, account: IssuerAccountView, creds: ClientCredentials
    ) -> IssueResult:
        """A 409 is never success: the lookup decides, and an unknown record goes to review."""
        try:
            found = await self._provider.find_by_reference(account, creds, document.reference_code)
        except ProviderTransient as exc:
            return await self._backoff(document, exc.retry_after, request_authorized_resend=False)
        if found is None:
            details: dict[str, Any] = {"provider_state": "none"}
        elif found.state == "failed":
            details = {"provider_state": "failed"}
            if found.provider_message is not None:
                details["provider_message"] = found.provider_message
        elif found.state == "draft":
            details = {"provider_state": "draft"}
        else:
            return await self._apply_lookup(document, found)
        return await self._fail(
            document,
            IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
            ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
            details,
            request_authorized_resend=False,
        )

    async def _date_rejected(
        self, document: DocumentRow, account: IssuerAccountView, creds: ClientCredentials
    ) -> IssueResult:
        """A synchronous ``issued_date_invalid``: look up, then resend once with today's date
        only when this 400 answered the first POST of the reference."""
        try:
            found = await self._provider.find_by_reference(account, creds, document.reference_code)
        except ProviderTransient as exc:
            return await self._backoff(document, exc.retry_after, request_authorized_resend=False)
        if found is not None:
            if found.state == "failed":
                return await self._fail(
                    document,
                    IssueErrorCode.PROVIDER_FAILED,
                    ReviewReason.PROVIDER_REJECTED,
                    failed_job_details(found.provider_message),
                    request_authorized_resend=False,
                )
            return await self._apply_lookup(document, found)
        async with self._uow_factory() as uow:
            current = await uow.documents.get(document.id)
        assert current is not None
        if current.lease_generation != document.lease_generation:
            return self._lease_lost(document)
        budget = {"post_count": current.post_count, "post_limit": current.post_limit}
        if (
            current.post_count == 1
            and current.date_resend_at is None
            and current.post_count < current.post_limit
        ):
            return await self._freeze_send(
                current, account, today_vn(self._clock.now()), date_resend=True
            )
        skipped = "earlier_post_unrecorded" if current.post_count >= 2 else "operator_grant_used"
        return await self._fail(
            current,
            IssueErrorCode.ISSUED_DATE_INVALID,
            ReviewReason.PROVIDER_REJECTED,
            {"code": ISSUED_DATE_INVALID, "date_resend_skipped": skipped, **budget},
            request_authorized_resend=False,
        )

    async def _date_resend_conflict(
        self, document: DocumentRow, account: IssuerAccountView, creds: ClientCredentials
    ) -> IssueResult:
        """409 on the date resend always goes to review; the lookup only records evidence."""
        details: dict[str, Any] = {"after_date_resend": True}
        try:
            found = await self._provider.find_by_reference(account, creds, document.reference_code)
        except ProviderTransient:
            details["lookup_state"] = "error"
        else:
            details["lookup_state"] = "none" if found is None else found.state
            if found is not None and found.provider_message is not None:
                details["provider_message"] = found.provider_message
        details["provider_state"] = details["lookup_state"]
        return await self._fail(
            document,
            IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
            ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
            details,
            request_authorized_resend=False,
        )

    async def _transient(self, document: DocumentRow, retry_after: timedelta | None) -> IssueResult:
        if document.attempts >= self._config.issue_max_attempts:
            return await self._fail(
                document,
                IssueErrorCode.MAX_ATTEMPTS_EXCEEDED,
                ReviewReason.MAX_ATTEMPTS_EXCEEDED,
                {
                    "attempts": document.attempts,
                    "issue_max_attempts": self._config.issue_max_attempts,
                },
            )
        return await self._backoff(document, retry_after)

    async def _backoff(
        self, document: DocumentRow, retry_after: timedelta | None, **values: Any
    ) -> IssueResult:
        now = self._clock.now()
        delay = retry_after if retry_after is not None else retry_delay(document.attempts)
        async with self._uow_factory() as uow:
            if not await uow.documents.finalize(
                document.id,
                document.lease_generation,
                next_attempt_at=now + delay,
                last_error_code=IssueErrorCode.TRANSIENT_ERROR.value,
                updated_at=now,
                **values,
            ):
                return self._lease_lost(document)
            await uow.commit()
        return IssueResult(document.id, "backoff")

    async def _quota(self, document: DocumentRow) -> IssueResult:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            if not await uow.documents.finalize(
                document.id,
                document.lease_generation,
                next_attempt_at=now + self._config.quota_backoff,
                attempts=max(document.attempts - 1, 0),
                last_error_code=IssueErrorCode.QUOTA_EXHAUSTED.value,
                updated_at=now,
            ):
                return self._lease_lost(document)
            await open_review(uow, document, ReviewReason.QUOTA_EXHAUSTED, {}, now)
            await uow.commit()
        logger.error("invoice_quota_exhausted", extra={"document_id": str(document.id)})
        self._count("invoice_quota_exhausted_total")
        return IssueResult(document.id, "quota")

    async def _rejected(self, document: DocumentRow, code: str) -> IssueResult:
        error = code if code in _REJECTION_CODES else IssueErrorCode.VALIDATION_ERROR.value
        return await self._fail(
            document,
            IssueErrorCode(error),
            ReviewReason.PROVIDER_REJECTED,
            {"code": code},
            request_authorized_resend=False,
        )

    async def _aborted(self, document: DocumentRow, reason: str) -> IssueResult:
        """Nothing was sent; the next claim looks the reference up first."""
        self._count("invoice_post_skipped_total", reason=reason)
        now = self._clock.now()
        async with self._uow_factory() as uow:
            if not await uow.documents.finalize(
                document.id, document.lease_generation, next_attempt_at=now, updated_at=now
            ):
                return self._lease_lost(document)
            await uow.commit()
        return IssueResult(document.id, f"skipped:{reason}")

    async def _fail(
        self,
        document: DocumentRow,
        error_code: IssueErrorCode,
        reason: ReviewReason,
        details: dict[str, Any],
        **values: Any,
    ) -> IssueResult:
        async with self._uow_factory() as uow:
            if not await fail_document(
                uow,
                document,
                document.lease_generation,
                error_code,
                reason,
                details,
                self._clock.now(),
                **values,
            ):
                return self._lease_lost(document)
            await uow.commit()
        return IssueResult(document.id, DocumentStatus.FAILED.value)

    # -- helpers ---------------------------------------------------------------------------

    def _lease_lost(self, document: DocumentRow) -> IssueResult:
        logger.warning("invoice_lease_lost", extra={"document_id": str(document.id)})
        self._count("invoice_lease_lost_total", loop="issue")
        return IssueResult(document.id, "lease_lost")

    async def _account(self, document: DocumentRow) -> IssuerAccountView:
        assert document.issuer_account_id is not None
        async with self._uow_factory() as uow:
            account = await uow.issuer_accounts.get_view(document.issuer_account_id)
        if account is None:
            raise LookupError("issuer account not found")
        return account

    async def _creds(self, account: IssuerAccountView) -> ClientCredentials:
        return await self._credentials.client_credentials(account.credential_ref)

    def _build(
        self, account: IssuerAccountView, document: DocumentRow, issued_date: date
    ) -> dict[str, Any]:
        assert document.document is not None
        snapshot = InvoiceDocument.from_json(document.document)
        return self._provider.build_create_payload(
            account, snapshot, document.reference_code, issued_date
        )

    def _count(self, name: str, **tags: str) -> None:
        increment_safely(self._metrics, name, tags)


def failed_job_details(message: str | None) -> dict[str, Any]:
    """A final ``Failed`` job is a definitive provider answer; its localised message is stored
    verbatim and never parsed."""
    details: dict[str, Any] = {"provider_job_state": "Failed"}
    if message is not None:
        details["provider_message"] = message
    return details
