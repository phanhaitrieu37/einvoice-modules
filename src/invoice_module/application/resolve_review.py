"""Operator resolutions of the review queue.

Every resolution takes the host_ref lock before the document lock (the order ``SubmitDraft`` and
``RecordSettledPayment`` use), refuses while a worker lease is live, and bumps the lease
generation so a stale worker write loses its compare-and-set. Provider lookups run outside the
transaction; the write then checks that the generation did not move since the read.

The reference code changes only through ``retry_with_new_reference``, which needs the operator's
note recording the reconciliation at the provider portal and a lookup that finds nothing.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from invoice_module.adapters.sqlalchemy.repositories.documents import (
    DEFAULT_POST_LIMIT,
    DocumentRow,
    approval_valid,
    snapshot_sha256,
)
from invoice_module.adapters.sqlalchemy.repositories.review_cases import ReviewCaseRow
from invoice_module.application.attach_draft import (
    attach_draft_to_documents,
    duplicate_hold_active,
    find_draft_for_document,
)
from invoice_module.application.issuance import (
    FinalizeResult,
    IssuedFacts,
    finalize_issued,
    missing_issued_facts,
)
from invoice_module.application.submit_draft import (
    SubmitDraft,
    SubmitDraftCommand,
    build_line_inputs,
)
from invoice_module.domain import state
from invoice_module.domain.document import DraftData, freeze_document, validate_buyer
from invoice_module.domain.enums import (
    DeliveryMode,
    DocumentStatus,
    Environment,
    ReviewReason,
    ReviewResolution,
)
from invoice_module.domain.errors import (
    DocumentInFlight,
    InvoiceError,
    ReferenceStillExists,
    ResolutionNotAllowed,
    ScopeMismatch,
)
from invoice_module.domain.money import check_declared_total
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.provider import (
    EInvoiceProvider,
    IssuerAccountView,
    ProviderInvoice,
)
from invoice_module.ports.unit_of_work import InvoiceUnitOfWork, InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

R = ReviewReason
S = DocumentStatus
A = ReviewResolution


class AmountMismatch(InvoiceError):
    pass


def _rules() -> dict[tuple[ReviewReason, DocumentStatus], frozenset[ReviewResolution]]:
    rules: list[tuple[set[ReviewReason], set[DocumentStatus], set[ReviewResolution]]] = [
        ({R.DRAFT_MISSING}, {S.NEEDS_DRAFT}, {A.ATTACH_DRAFT, A.WAIVE}),
        ({R.AMOUNT_MISMATCH}, {S.BLOCKED}, {A.ATTACH_DRAFT, A.WAIVE}),
        (
            {R.ISSUER_ACCOUNT_UNAVAILABLE, R.ISSUE_DATE_CROSSED_DAY, R.DUPLICATE_SETTLEMENT},
            {S.BLOCKED},
            {A.RETRY, A.WAIVE},
        ),
        ({R.DRAFT_HELD}, {S.BLOCKED}, {A.RETRY, A.ATTACH_DRAFT, A.WAIVE}),
        ({R.QUOTA_EXHAUSTED}, {S.QUEUED, S.APPROVED}, {A.RETRY}),
        (
            {R.PROVIDER_REJECTED, R.PROVIDER_FAILED, R.LOOKUP_MISMATCH},
            {S.FAILED},
            {A.RETRY, A.ATTACH_DRAFT, A.RETRY_WITH_NEW_REFERENCE, A.MARK_EXTERNAL, A.WAIVE},
        ),
        (
            {
                R.PROVIDER_TIMEOUT,
                R.AUTH_FAILED,
                R.MAX_ATTEMPTS_EXCEEDED,
                R.DOCUMENT_EXISTED_UNRESOLVED,
            },
            {S.FAILED},
            {A.RETRY, A.RETRY_WITH_NEW_REFERENCE, A.MARK_EXTERNAL, A.WAIVE},
        ),
        ({R.STALE_REFERENCE_FOUND}, set(S), {A.ACKNOWLEDGE}),
        ({R.STALE_REFERENCE_FOUND}, {S.FAILED}, {A.MARK_EXTERNAL}),
        ({R.CORRECTION_REJECTED}, {S.FAILED}, {A.RETRY, A.WAIVE}),
    ]
    table: dict[tuple[ReviewReason, DocumentStatus], set[ReviewResolution]] = {}
    for reasons, statuses, resolutions in rules:
        for reason in reasons:
            for status in statuses:
                table.setdefault((reason, status), set()).update(resolutions)
    return {key: frozenset(value) for key, value in table.items()}


# The single source of the reason x document status -> resolution matrix (docs copy it).
ALLOWED_RESOLUTIONS = _rules()
_TERMINAL = frozenset({S.ISSUED, S.REPLACED, S.CANCELLED_EXTERNALLY})
_HOLD_RELEASING = frozenset({R.ISSUER_ACCOUNT_UNAVAILABLE, R.ISSUE_DATE_CROSSED_DAY, R.DRAFT_HELD})


def allowed_resolutions(reason: ReviewReason, status: DocumentStatus) -> frozenset[A]:
    return ALLOWED_RESOLUTIONS.get((reason, status), frozenset())


@dataclass(frozen=True)
class ResolutionResult:
    case_id: UUID
    document_id: UUID
    document_status: DocumentStatus
    case_resolved: bool
    reference_code: str


@dataclass(frozen=True)
class _Request:
    case_id: UUID
    resolution: ReviewResolution
    reason: ReviewReason
    actor: str
    note: str | None
    invoice_number: str | None
    tax_authority_code: str | None
    provider_invoice_id: str | None
    issued_date: date | None
    draft: SubmitDraftCommand | None
    draft_data: DraftData | None


class ResolveReview:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        provider: EInvoiceProvider | None,
        credentials: CredentialResolver | None,
        clock: Clock,
        metrics: MetricsSink | None = None,
        submit_draft: SubmitDraft | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._credentials = credentials
        self._clock = clock
        self._metrics = metrics or NoOpMetricsSink()
        self._submit_draft = submit_draft or SubmitDraft(uow_factory, clock)

    async def execute(
        self,
        case_id: UUID,
        resolution: ReviewResolution | str,
        actor: str,
        note: str | None = None,
        *,
        reason: ReviewReason | str | None = None,
        invoice_number: str | None = None,
        tax_authority_code: str | None = None,
        provider_invoice_id: str | None = None,
        issued_date: date | None = None,
        draft: SubmitDraftCommand | None = None,
    ) -> ResolutionResult:
        resolution = ReviewResolution(resolution)
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor is required")
        actor = actor.strip()
        note_present = isinstance(note, str) and bool(note.strip())
        if resolution in _NOTE_REQUIRED and not note_present:
            raise ValueError(f"{resolution.value} requires a note")
        if resolution == A.MARK_EXTERNAL:
            if not isinstance(invoice_number, str) or not invoice_number.strip():
                raise ValueError("mark_external requires the invoice number from the portal")
            if not isinstance(issued_date, date) or isinstance(issued_date, datetime):
                raise ValueError("mark_external requires the provider issue date as a date")
        if resolution == A.ATTACH_DRAFT and draft is None:
            raise ValueError("attach_draft requires a draft")

        async with self._uow_factory() as uow:
            case = await uow.review_cases.get(case_id)
            if case is None:
                raise ResolutionNotAllowed("case_not_found")
            document = await uow.documents.get(case.document_id)
        assert document is not None
        target = ReviewReason(reason) if reason is not None else ReviewReason(case.reason)
        if target == R.DUPLICATE_SETTLEMENT and resolution == A.RETRY and not note_present:
            raise ValueError("retrying a duplicate settlement requires a note")
        request = _Request(
            case_id,
            resolution,
            target,
            actor,
            note if note_present else None,
            invoice_number.strip() if invoice_number else None,
            tax_authority_code,
            provider_invoice_id,
            issued_date,
            draft,
            None,
        )
        now = self._clock.now()
        self._check(case, document, request, now)
        draft_data = self._draft_data(document, request)
        request = _Request(**(request.__dict__ | {"draft_data": draft_data}))

        lookup: ProviderInvoice | None = None
        if document.status == S.FAILED and resolution in (
            A.RETRY,
            A.ATTACH_DRAFT,
            A.RETRY_WITH_NEW_REFERENCE,
        ):
            lookup = await self._lookup(document)
            if resolution == A.RETRY_WITH_NEW_REFERENCE and lookup is not None:
                raise ReferenceStillExists(document.reference_code)
            if (
                resolution == A.ATTACH_DRAFT
                and lookup is not None
                and lookup.state
                in (
                    "success",
                    "pending",
                )
            ):
                raise ResolutionNotAllowed(f"provider_record_{lookup.state}")

        async with self._uow_factory() as uow:
            current = await self._lock(uow, document, case_id, request)
            status = await self._apply(uow, current, request, lookup)
            await uow.commit()
        if resolution == A.RETRY_WITH_NEW_REFERENCE:
            status = await self._requeue_rotated(document, request)
        async with self._uow_factory() as uow:
            final_case = await uow.review_cases.get(case_id)
            final_document = await uow.documents.get(document.id)
        assert final_case is not None and final_document is not None
        logger.info(
            "invoice_review_resolved",
            extra={
                "case_id": str(case_id),
                "document_id": str(document.id),
                "reason": target.value,
                "resolution": resolution.value,
                "actor": actor,
            },
        )
        increment_safely(
            self._metrics,
            "invoice_review_resolutions_total",
            {"reason": target.value, "resolution": resolution.value},
        )
        return ResolutionResult(
            case_id,
            document.id,
            status,
            final_case.status == "resolved",
            final_document.reference_code,
        )

    # -- checks ----------------------------------------------------------------------------

    def _check(
        self, case: ReviewCaseRow, document: DocumentRow, request: _Request, now: datetime
    ) -> None:
        # A live worker lease wins over everything else: nothing is decided while a claim runs.
        if document.lease_until is not None and document.lease_until > now:
            raise DocumentInFlight(str(document.id))
        if case.status != "open" or request.reason.value not in case.open_reasons():
            raise ResolutionNotAllowed(f"reason_not_open:{request.reason.value}")
        if request.resolution not in allowed_resolutions(request.reason, document.status):
            raise ResolutionNotAllowed(
                f"{request.resolution.value} not allowed for {request.reason.value}"
                f" on {document.status.value}"
            )
        if request.resolution == A.WAIVE and document.status in _TERMINAL:
            raise ResolutionNotAllowed(f"waive not allowed on {document.status.value}")
        if (
            request.resolution == A.MARK_EXTERNAL
            and document.environment == Environment.LIVE.value
            and not (request.tax_authority_code or "").strip()
        ):
            raise ValueError("a live invoice requires the tax authority code")
        if request.draft is not None and request.draft.merchant_id != document.merchant_id:
            raise ScopeMismatch("draft merchant does not match the document merchant")

    def _draft_data(self, document: DocumentRow, request: _Request) -> DraftData | None:
        """Corrected content for a document that already consumed its draft: validated in
        full, and equal to the settled amount, before any database write."""
        if request.resolution != A.ATTACH_DRAFT or document.status not in (S.FAILED, S.BLOCKED):
            return None
        if request.reason not in (
            R.PROVIDER_REJECTED,
            R.PROVIDER_FAILED,
            R.LOOKUP_MISMATCH,
            R.DRAFT_HELD,
        ):
            return None
        cmd = request.draft
        assert cmd is not None
        buyer = validate_buyer(cmd.buyer) if cmd.buyer is not None else None
        lines = build_line_inputs(cmd.lines)
        check_declared_total(cmd.declared_total_vnd, lines)
        if cmd.declared_total_vnd != document.settled_amount_vnd:
            raise AmountMismatch(
                f"declared total {cmd.declared_total_vnd} differs from the settled amount"
            )
        return DraftData(buyer, lines, cmd.declared_total_vnd, cmd.notes)

    async def _lookup(self, document: DocumentRow) -> ProviderInvoice | None:
        if self._provider is None or self._credentials is None:
            raise ResolutionNotAllowed("provider_required")
        account = await self._account(document)
        creds = await self._credentials.client_credentials(account.credential_ref)
        return await self._provider.find_by_reference(account, creds, document.reference_code)

    async def _lock(
        self, uow: InvoiceUnitOfWork, read: DocumentRow, case_id: UUID, request: _Request
    ) -> DocumentRow:
        await uow.lock_host_ref(read.tenant_id, read.host_ref_type, read.host_ref_id)
        current = await uow.documents.lock_for_operator(read.id)
        now = self._clock.now()
        if current.lease_until is not None and current.lease_until > now:
            raise DocumentInFlight(str(read.id))
        if current.lease_generation != read.lease_generation:
            raise DocumentInFlight(str(read.id))
        case = await uow.review_cases.get(case_id, for_update=True)
        assert case is not None
        self._check(case, current, request, now)
        return current

    # -- resolutions -----------------------------------------------------------------------

    async def _apply(
        self,
        uow: InvoiceUnitOfWork,
        document: DocumentRow,
        request: _Request,
        lookup: ProviderInvoice | None,
    ) -> DocumentStatus:
        resolution = request.resolution
        if resolution in (A.WAIVE, A.ACKNOWLEDGE):
            await self._clear(uow, document, request)
            await uow.documents.bump_generation(document.id)
            return document.status
        if resolution == A.RETRY and request.reason == R.QUOTA_EXHAUSTED:
            return await self._retry_quota(uow, document, request)
        if resolution == A.MARK_EXTERNAL:
            return await self._mark_external(uow, document, request)
        if resolution == A.RETRY_WITH_NEW_REFERENCE:
            return await self._rotate(uow, document, request)
        if resolution == A.ATTACH_DRAFT:
            return await self._attach(uow, document, request)
        if document.status == S.FAILED:
            return await self._retry_failed(uow, document, request, lookup)
        return await self._retry_blocked(uow, document, request)

    async def _retry_quota(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request
    ) -> DocumentStatus:
        """The quota was topped up: the document is claimable again with exactly one more POST.
        Its status and approval stay; the next claim looks the reference up before it sends."""
        now = self._clock.now()
        await self._clear(uow, document, request)
        await uow.documents.operator_update(document.id, now, next_attempt_at=now, attempts=0)
        await uow.documents.grant_post(document.id)
        return document.status

    async def _retry_failed(
        self,
        uow: InvoiceUnitOfWork,
        document: DocumentRow,
        request: _Request,
        lookup: ProviderInvoice | None,
    ) -> DocumentStatus:
        now = self._clock.now()
        if lookup is not None and lookup.state == "success":
            if document.delivery_mode == DeliveryMode.MANUAL and not approval_valid(document):
                raise ResolutionNotAllowed("issued_without_approval")
            facts = IssuedFacts(
                lookup.invoice_number,
                lookup.tax_authority_code,
                lookup.provider_invoice_id,
                lookup.issued_date,
            )
            missing = missing_issued_facts(document, facts)
            if missing:
                raise ResolutionNotAllowed(f"lookup_incomplete:{','.join(missing)}")
            if lookup.total_amount_vnd is not None and lookup.total_amount_vnd != (
                document.total_vnd
            ):
                raise ResolutionNotAllowed("lookup_total_mismatch")
            await self._clear(uow, document, request)
            result = await finalize_issued(
                uow,
                document,
                document.lease_generation,
                invoice_number=facts.invoice_number,
                tax_authority_code=facts.tax_authority_code,
                provider_invoice_id=facts.provider_invoice_id,
                provider_issued_date=facts.provider_issued_date,
                issued_at=now,
                lookup_total_vnd=lookup.total_amount_vnd,
            )
            if result != FinalizeResult.ISSUED:
                raise ResolutionNotAllowed(f"finalize_{result.value}")
            await uow.documents.bump_generation(document.id)
            return S.ISSUED
        if lookup is not None and lookup.state == "pending":
            await self._clear(uow, document, request)
            state.ensure_transition(document.status, S.PENDING_PROVIDER)
            await uow.documents.operator_update(
                document.id,
                now,
                status=S.PENDING_PROVIDER.value,
                tracking_code=lookup.tracking_code or document.tracking_code,
                pending_since=now,
                poll_count=0,
                next_attempt_at=now,
                last_error_code=None,
            )
            return S.PENDING_PROVIDER
        if lookup is not None and lookup.state == "draft":
            raise ResolutionNotAllowed("provider_record_draft")
        if await duplicate_hold_active(uow, document.id):
            # A failed document cannot go back to ``blocked``; nothing changes until the
            # duplicate is decided.
            raise ResolutionNotAllowed("duplicate_hold_active")
        await self._clear(uow, document, request)
        await self._audit_attempts(uow, document, request, now)
        if lookup is None:
            # The earlier outcome is unknown: keep the frozen request; the worker looks the
            # reference up again before it resends it (or refreezes for a new day).
            if document.delivery_mode == DeliveryMode.MANUAL and approval_valid(document):
                status = await self._requeue(uow, document, now, request.case_id, target=S.APPROVED)
            else:
                status = await self._requeue(uow, document, now, request.case_id, target=S.QUEUED)
        else:
            # A known failed record: a new request is frozen and sent once; the permission to
            # resend survives a crash between that freeze and the POST.
            status = await self._requeue(
                uow,
                document,
                now,
                request.case_id,
                target=S.QUEUED,
                request=None,
                request_frozen_at=None,
                operator_retry_at=now,
            )
        await uow.documents.grant_post(document.id)
        return status

    async def _retry_blocked(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request
    ) -> DocumentStatus:
        now = self._clock.now()
        reason = request.reason
        if reason == R.ISSUER_ACCOUNT_UNAVAILABLE:
            account = await uow.issuer_accounts.get_active(
                document.tenant_id, document.merchant_id, Environment(document.environment)
            )
            if account is None:
                raise ResolutionNotAllowed("no_active_issuer_account")
            await self._clear(uow, document, request)
            await uow.documents.set_issuer_account(document.id, account.id)
            current = await uow.documents.get(document.id)
            assert current is not None
            return await self._evaluate_draft(uow, current, request, now)
        if reason == R.DUPLICATE_SETTLEMENT:
            await self._clear(uow, document, request)
            if await uow.review_cases.open_reasons(document.id):
                await uow.documents.bump_generation(document.id)
                return document.status
            if await duplicate_hold_active(uow, document.id):
                await uow.documents.bump_generation(document.id)
                return document.status
            if document.document is not None:
                return await self._requeue(uow, document, now, request.case_id, target=S.QUEUED)
            return await self._evaluate_draft(uow, document, request, now)
        await self._clear(uow, document, request)
        if await duplicate_hold_active(uow, document.id):
            await uow.documents.bump_generation(document.id)
            return document.status
        if reason == R.DRAFT_HELD:
            # Never sent (a hold needs a NULL send mark): it only goes back for a new approval.
            state.ensure_transition(document.status, S.AWAITING_APPROVAL)
            await uow.documents.operator_update(
                document.id, now, status=S.AWAITING_APPROVAL.value, next_attempt_at=None
            )
            return S.AWAITING_APPROVAL
        return await self._requeue(uow, document, now, request.case_id, target=S.QUEUED)

    async def _evaluate_draft(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request, now: datetime
    ) -> DocumentStatus:
        """Look for the draft with the shared rule, attach it to this document only, or wait
        for one; a duplicate hold keeps the document blocked either way."""
        draft = await find_draft_for_document(uow, document)
        if draft is not None:
            outcomes = await attach_draft_to_documents(
                uow,
                draft,
                uow.issuer_accounts.get_active,
                now,
                documents=[document],
                actor=request.actor,
            )
            await uow.documents.bump_generation(document.id)
            return S(outcomes[0].status) if outcomes else document.status
        if await duplicate_hold_active(uow, document.id):
            await uow.documents.bump_generation(document.id)
            return document.status
        state.ensure_transition(document.status, S.NEEDS_DRAFT)
        await uow.documents.operator_update(
            document.id, now, status=S.NEEDS_DRAFT.value, next_attempt_at=None
        )
        return S.NEEDS_DRAFT

    async def _attach(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request
    ) -> DocumentStatus:
        now = self._clock.now()
        if request.draft_data is not None:
            account = await uow.issuer_accounts.get_view(document.issuer_account_id)
            assert account is not None
            await self._clear(uow, document, request)
            await self._audit_attempts(uow, document, request, now)
            status = await repair_failed_document(
                uow,
                document,
                request.draft_data,
                account,
                now,
                actor=request.actor,
                case_id=request.case_id,
            )
            if document.status == S.FAILED:
                await uow.documents.grant_post(document.id)
            return status
        assert request.draft is not None
        await self._submit_draft.apply(uow, request.draft, actor=request.actor, note=request.note)
        current = await uow.documents.get(document.id)
        assert current is not None
        if request.reason.value in await uow.review_cases.open_reasons(document.id):
            if request.reason == R.AMOUNT_MISMATCH:
                raise AmountMismatch("the draft total still differs from the settled amount")
            raise ResolutionNotAllowed("draft_not_attached")
        await uow.documents.bump_generation(document.id)
        return current.status

    async def _mark_external(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request
    ) -> DocumentStatus:
        """The operator confirms the invoice exists at the portal and copies its number and
        issue date; for a manual document that confirmation is also the approval."""
        now = self._clock.now()
        if document.delivery_mode == DeliveryMode.MANUAL and not approval_valid(document):
            assert document.document is not None
            await uow.documents.record_approval(
                document.id,
                request.actor,
                now,
                snapshot_sha256(document.document),
                document.reference_code,
            )
            refreshed = await uow.documents.get(document.id)
            assert refreshed is not None
            document = refreshed
        await self._clear(uow, document, request)
        result = await finalize_issued(
            uow,
            document,
            document.lease_generation,
            invoice_number=request.invoice_number,
            tax_authority_code=request.tax_authority_code,
            provider_invoice_id=request.provider_invoice_id,
            provider_issued_date=request.issued_date,
            issued_at=now,
            lookup_total_vnd=None,
        )
        if result != FinalizeResult.ISSUED:
            raise ResolutionNotAllowed(f"finalize_{result.value}")
        await uow.documents.bump_generation(document.id)
        return S.ISSUED

    async def _rotate(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request
    ) -> DocumentStatus:
        """First transaction of a rotation: the new reference is durable before the document
        can be claimed again (the requeue is a second transaction)."""
        now = self._clock.now()
        await uow.documents.clear_approval(
            document.id, request.case_id, request.actor, "reference_rotated", now
        )
        await uow.review_cases.append_detail(
            request.case_id,
            "previous_references",
            {
                "reference_code": document.reference_code,
                "rotated_at": now.isoformat(),
                "reconciled_by": request.actor,
                "reconciliation_note": request.note,
            },
        )
        await self._audit_attempts(uow, document, request, now)
        await uow.documents.operator_update(
            document.id,
            now,
            reference_code=str(uuid4()),
            request=None,
            request_frozen_at=None,
            request_authorized_resend=False,
            operator_retry_at=None,
            tracking_code=None,
            attempts=0,
            send_started_at=None,
            post_count=0,
            post_limit=DEFAULT_POST_LIMIT,
            date_resend_at=None,
        )
        return document.status

    async def _requeue_rotated(self, read: DocumentRow, request: _Request) -> DocumentStatus:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            await uow.lock_host_ref(read.tenant_id, read.host_ref_type, read.host_ref_id)
            document = await uow.documents.lock_for_operator(read.id)
            if document.status != S.FAILED or document.reference_code == read.reference_code:
                raise ResolutionNotAllowed("rotation_not_recorded")
            await self._clear(uow, document, request)
            if await duplicate_hold_active(uow, document.id):
                await uow.commit()
                return document.status
            status = await self._requeue(uow, document, now, request.case_id, target=S.QUEUED)
            await uow.commit()
        return status

    # -- shared writes ---------------------------------------------------------------------

    async def _clear(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request
    ) -> None:
        await uow.review_cases.clear_reason(
            request.case_id,
            request.reason.value,
            request.resolution.value,
            request.actor,
            request.note,
            self._clock.now(),
        )

    async def _requeue(
        self,
        uow: InvoiceUnitOfWork,
        document: DocumentRow,
        now: datetime,
        case_id: UUID,
        *,
        target: DocumentStatus,
        **values: Any,
    ) -> DocumentStatus:
        if target == S.QUEUED and document.delivery_mode == DeliveryMode.MANUAL:
            # A manual document back in the queue always needs a fresh approval; the case being
            # resolved keeps the audit even when this resolution just closed it.
            await uow.documents.clear_approval(
                document.id, case_id, "system:requeue", "requeued", now
            )
        state.ensure_transition(document.status, target)
        await uow.documents.operator_update(
            document.id,
            now,
            status=target.value,
            queued_at=now if target == S.QUEUED else document.queued_at,
            next_attempt_at=now,
            attempts=0,
            last_error_code=None,
            **values,
        )
        return target

    async def _audit_attempts(
        self, uow: InvoiceUnitOfWork, document: DocumentRow, request: _Request, now: datetime
    ) -> None:
        await uow.review_cases.append_detail(
            request.case_id,
            "previous_attempts",
            {
                "attempts": document.attempts,
                "post_count": document.post_count,
                "post_limit": document.post_limit,
                "resolution": request.resolution.value,
                "by": request.actor,
                "at": now.isoformat(),
            },
        )

    async def _account(self, document: DocumentRow) -> IssuerAccountView:
        assert document.issuer_account_id is not None
        async with self._uow_factory() as uow:
            account = await uow.issuer_accounts.get_view(document.issuer_account_id)
        assert account is not None
        return account


_NOTE_REQUIRED = frozenset({A.WAIVE, A.ACKNOWLEDGE, A.RETRY_WITH_NEW_REFERENCE})


async def repair_failed_document(
    uow: InvoiceUnitOfWork,
    document: DocumentRow,
    draft_data: DraftData,
    account: IssuerAccountView,
    now: datetime,
    *,
    actor: str,
    case_id: UUID,
) -> DocumentStatus:
    """Rebuild the snapshot of this document only, from corrected content.

    The draft row is never touched: it may already be reopened for another checkout of the
    same host reference. The request is cleared so the next claim freezes a new one, and a
    manual document loses its approval (the content changed).
    """
    check_declared_total(draft_data.declared_total_vnd, draft_data.lines)
    if draft_data.declared_total_vnd != document.settled_amount_vnd:
        raise AmountMismatch("declared total differs from the settled amount")
    frozen = freeze_document(draft_data, account)
    await uow.documents.clear_approval(document.id, case_id, actor, "content_repaired", now)
    await uow.review_cases.append_detail(
        case_id,
        "repairs",
        {
            "actor": actor,
            "at": now.isoformat(),
            "previous_snapshot_sha256": (
                snapshot_sha256(document.document) if document.document is not None else None
            ),
        },
    )
    if await duplicate_hold_active(uow, document.id):
        raise ResolutionNotAllowed("duplicate_hold_active")
    state.ensure_transition(document.status, S.QUEUED)
    await uow.documents.operator_update(
        document.id,
        now,
        document=frozen.to_json(),
        total_vnd=frozen.total_vnd,
        buyer_source=frozen.buyer_source.value,
        request=None,
        request_frozen_at=None,
        operator_retry_at=now,
        attempts=0,
        status=S.QUEUED.value,
        queued_at=now,
        next_attempt_at=now,
        last_error_code=None,
    )
    return S.QUEUED


__all__ = [
    "ALLOWED_RESOLUTIONS",
    "AmountMismatch",
    "ResolutionResult",
    "ResolveReview",
    "allowed_resolutions",
    "repair_failed_document",
]
