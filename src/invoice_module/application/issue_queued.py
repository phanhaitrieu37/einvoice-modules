"""Issue loop: freeze the create request just before sending, send it once per POST grant, and
record the outcome with a compare-and-set on the claim's lease generation.

Every POST goes through the send-boundary hook, where ``mark_post`` is the only grant of one
transport request; the reference has a POST budget (two automatic POSTs by default, one more
per operator grant), so no path can send a third automatic create for a reference. Any POST
whose outcome is unknown is followed by a lookup before anything else is sent. The reference
code never changes here.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
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
# Provider rejection code -> error recorded on the document; any other code is a validation
# error. The code is matched exactly as the adapter passes it: only the synchronous 400 date
# rejection arrives as ``issued_date_invalid`` and it takes the date path before this table is
# read, while a 2xx carrying ``ISSUED_DATE_INVALID`` may have queued a job and must not reach the
# resend path, so letter case is never normalised and that code stays a validation error.
PROVIDER_CODE_TO_ERROR: Mapping[str, IssueErrorCode] = MappingProxyType(
    {
        "REGISTRATION_NOT_COMPLETE": IssueErrorCode.REGISTRATION_NOT_APPROVED,
        "BILLING_IS_UNPAID": IssueErrorCode.REGISTRATION_NOT_APPROVED,
        "EINVOICE_ACCOUNT_NOT_FOUND": IssueErrorCode.REGISTRATION_NOT_APPROVED,
        "REGISTRATION_NOT_TAX_APPROVED": IssueErrorCode.REGISTRATION_NOT_APPROVED,
        "INVOICE_DATE_BEFORE_TAX_APPROVAL": IssueErrorCode.REGISTRATION_NOT_APPROVED,
        "SELLER_STORE_NOT_FOUND": IssueErrorCode.SELLER_STORE_INVALID,
        "SELLER_STORE_INACTIVE": IssueErrorCode.SELLER_STORE_INVALID,
    }
)


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
                logger.error(
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
            entry = None
            if document.approved_at is not None:
                entry = await uow.documents.clear_approval(
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
        _log_approval_invalidated(document, entry)
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
            entry = await uow.documents.clear_approval(
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
        _log_approval_invalidated(document, entry)
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
        self,
        document: DocumentRow,
        account: IssuerAccountView,
        issued_date: date,
        *,
        after_date_rejection: bool = False,
    ) -> IssueResult | None:
        """The issue date would differ from the settlement date (or from the frozen request).

        ``after_date_rejection``: the provider refused the earlier POST for its date, a
        definitive answer, so any permission to resend that POST is spent; the details record
        which POST of which reference it was.
        """
        action = account.issue_date_cross_day_action
        if action != CrossDayAction.ISSUE and not await self._cross_day_confirmed(document):
            now = self._clock.now()
            details: dict[str, Any] = {
                "settled_date": today_vn(document.settled_at).isoformat(),
                "frozen_issued_date": (
                    document.issued_date.isoformat() if document.issued_date else None
                ),
                "issue_date": issued_date.isoformat(),
            }
            values: dict[str, Any] = {}
            if after_date_rejection:
                details["after_date_rejection"] = True
                details |= _answered_post(document, document.post_count)
                values["request_authorized_resend"] = False
            async with self._uow_factory() as uow:
                state.ensure_transition(document.status, DocumentStatus.BLOCKED)
                if not await uow.documents.finalize(
                    document.id,
                    document.lease_generation,
                    status=DocumentStatus.BLOCKED.value,
                    next_attempt_at=None,
                    updated_at=now,
                    **values,
                ):
                    return self._lease_lost(document)
                await open_review(uow, document, ReviewReason.ISSUE_DATE_CROSSED_DAY, details, now)
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
        except QuotaExhausted as exc:
            return await self._quota(document, str(exc), document.post_count + guard.granted)
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
                            **_answered_post(document, document.post_count + 1),
                        },
                        request_authorized_resend=False,
                    )
                return await self._date_rejected(document, account, creds)
            return await self._rejected(
                document, exc.code, document.post_count + guard.granted if guard.granted else None
            )
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
        if found.state in ("draft", "cancelled"):
            # A record under our reference that is not an issued invoice: a resend could only
            # answer 409, so an operator decides.
            return await self._fail(
                document,
                IssueErrorCode.DOCUMENT_EXISTED_UNRESOLVED,
                ReviewReason.DOCUMENT_EXISTED_UNRESOLVED,
                {"provider_state": found.state},
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
        budget = _answered_post(current, current.post_count) | {"post_limit": current.post_limit}
        if (
            current.post_count == 1
            and current.date_resend_at is None
            and current.post_count < current.post_limit
        ):
            today = today_vn(self._clock.now())
            # The resend goes through the cross-day policy like every other send.
            if current.delivery_mode == DeliveryMode.AUTO and today != today_vn(current.settled_at):
                blocked = await self._cross_day(current, account, today, after_date_rejection=True)
                if blocked is not None:
                    return blocked
            return await self._freeze_send(current, account, today, date_resend=True)
        skipped = await self._date_resend_skipped(current)
        return await self._fail(
            current,
            IssueErrorCode.ISSUED_DATE_INVALID,
            ReviewReason.PROVIDER_REJECTED,
            {"code": ISSUED_DATE_INVALID, "date_resend_skipped": skipped, **budget},
            request_authorized_resend=False,
        )

    async def _date_resend_skipped(self, current: DocumentRow) -> str:
        """Why a date refusal gets no resend, named from what the database recorded."""
        if current.date_resend_at is not None:
            return "date_resend_used"
        if current.post_count < 2:
            return "operator_grant_used"
        # Only the POST right before this one, under the same reference, counts: any POST in
        # between may be unanswered, and a rotated reference starts its count again.
        previous = _answered_post(current, current.post_count - 1)
        async with self._uow_factory() as uow:
            rejected_before = await uow.review_cases.reason_recorded(
                current.id,
                ReviewReason.ISSUE_DATE_CROSSED_DAY.value,
                {"after_date_rejection": True} | previous,
            ) or await uow.review_cases.reason_recorded(
                current.id, ReviewReason.PROVIDER_REJECTED.value, previous
            )
        # Otherwise the POST before this one has no recorded answer.
        return "earlier_post_rejected" if rejected_before else "earlier_post_unrecorded"

    async def _date_resend_conflict(
        self, document: DocumentRow, account: IssuerAccountView, creds: ClientCredentials
    ) -> IssueResult:
        """409 on the date resend always goes to review; the lookup only records evidence."""
        details: dict[str, Any] = {"after_date_resend": True}
        try:
            found = await self._provider.find_by_reference(account, creds, document.reference_code)
        except (ProviderTransient, ProviderAuthFailed):
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

    async def _quota(self, document: DocumentRow, code: str, post_count: int) -> IssueResult:
        """The quota does not come back by itself: the document keeps its status (and a manual
        approval) but is never claimed again until an operator retries after a top-up."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            if not await uow.documents.finalize(
                document.id,
                document.lease_generation,
                next_attempt_at=None,
                last_error_code=IssueErrorCode.QUOTA_EXHAUSTED.value,
                updated_at=now,
            ):
                return self._lease_lost(document)
            await open_review(
                uow,
                document,
                ReviewReason.QUOTA_EXHAUSTED,
                {"code": code, "post_count": post_count, "post_limit": document.post_limit},
                now,
            )
            await uow.commit()
        logger.error(
            "invoice_quota_exhausted",
            extra={"document_id": str(document.id), "post_count": post_count},
        )
        self._count("invoice_quota_exhausted_total")
        return IssueResult(document.id, "quota")

    async def _rejected(self, document: DocumentRow, code: str, post: int | None) -> IssueResult:
        """``post`` is the POST of the reference this rejection answered, ``None`` when no POST
        was sent (the refusal came before the send boundary)."""
        details: dict[str, Any] = {"code": code}
        if post is not None:
            details |= _answered_post(document, post)
        return await self._fail(
            document,
            PROVIDER_CODE_TO_ERROR.get(code, IssueErrorCode.VALIDATION_ERROR),
            ReviewReason.PROVIDER_REJECTED,
            details,
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


def _log_approval_invalidated(document: DocumentRow, entry: dict[str, Any] | None) -> None:
    """The log keeps the whole dropped approval, so the audit survives even without a case."""
    if entry is None:
        return
    logger.warning(
        "invoice_approval_invalidated",
        extra={
            "document_id": str(document.id),
            "approved_by": entry["approved_by"],
            "approved_at": entry["approved_at"],
            "approved_snapshot_sha256": entry["approved_snapshot_sha256"],
            "approved_reference_code": entry["approved_reference_code"],
            "reason": entry["reason"],
        },
    )


def _answered_post(document: DocumentRow, post_count: int) -> dict[str, Any]:
    """Which POST of which reference a recorded provider answer belongs to."""
    return {"post_count": post_count, "reference_code": document.reference_code}


def failed_job_details(message: str | None) -> dict[str, Any]:
    """A final ``Failed`` job is a definitive provider answer; its localised message is stored
    verbatim and never parsed."""
    details: dict[str, Any] = {"provider_job_state": "Failed"}
    if message is not None:
        details["provider_message"] = message
    return details
