from dataclasses import dataclass

from invoice_module.application import DiscardDraft, RecordSettledPayment, SubmitDraft
from invoice_module.application.approve_draft_invoice import ApproveDraftInvoice
from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.application.escalate_overdue import EscalateOverdue
from invoice_module.application.fetch_artifacts import FetchArtifacts
from invoice_module.application.hold_draft_invoice import HoldDraftInvoice
from invoice_module.application.issue_queued import IssueQueued
from invoice_module.application.mark_cancelled_externally import MarkCancelledExternally
from invoice_module.application.poll_pending import PollPending
from invoice_module.application.queries import (
    GetDocumentStatus,
    GetDraftForApproval,
    ListOpenReviews,
    ListPendingApprovals,
)
from invoice_module.application.resolve_review import ResolveReview
from invoice_module.ports.artifacts import ArtifactStore
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink
from invoice_module.ports.provider import EInvoiceProvider
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory


@dataclass(frozen=True)
class InvoiceModule:
    """Use cases that need the provider are ``None`` when the module is built without one, so a
    command-line tool can approve, hold and query with only the database."""

    uow_factory: InvoiceUnitOfWorkFactory
    clock: Clock
    metrics: MetricsSink
    submit_draft: SubmitDraft
    discard_draft: DiscardDraft
    record_settled_payment: RecordSettledPayment
    config: InvoiceModuleConfig
    issue_queued: IssueQueued | None
    poll_pending: PollPending | None
    fetch_artifacts: FetchArtifacts | None
    escalate_overdue: EscalateOverdue | None
    resolve_review: ResolveReview | None
    approve_draft_invoice: ApproveDraftInvoice
    hold_draft_invoice: HoldDraftInvoice
    mark_cancelled_externally: MarkCancelledExternally
    get_document_status: GetDocumentStatus
    list_open_reviews: ListOpenReviews
    list_pending_approvals: ListPendingApprovals
    get_draft_for_approval: GetDraftForApproval


def build_invoice_module(
    uow_factory: InvoiceUnitOfWorkFactory,
    clock: Clock,
    metrics: MetricsSink | None = None,
    *,
    provider: EInvoiceProvider | None = None,
    credentials: CredentialResolver | None = None,
    artifact_store: ArtifactStore | None = None,
    config: InvoiceModuleConfig | None = None,
) -> InvoiceModule:
    metric_sink = metrics or NoOpMetricsSink()
    settings = config or InvoiceModuleConfig()
    networked = provider is not None and credentials is not None
    submit_draft = SubmitDraft(uow_factory, clock)
    return InvoiceModule(
        uow_factory,
        clock,
        metric_sink,
        submit_draft,
        DiscardDraft(uow_factory, clock, metric_sink),
        RecordSettledPayment(uow_factory, clock, metric_sink),
        settings,
        issue_queued=(
            IssueQueued(uow_factory, provider, credentials, clock, metric_sink, settings)
            if networked
            else None
        ),
        poll_pending=(
            PollPending(uow_factory, provider, credentials, clock, metric_sink, settings)
            if networked
            else None
        ),
        fetch_artifacts=(
            FetchArtifacts(
                uow_factory, provider, credentials, artifact_store, clock, metric_sink, settings
            )
            if networked and artifact_store is not None
            else None
        ),
        escalate_overdue=(
            EscalateOverdue(uow_factory, provider, credentials, clock, metric_sink, settings)
            if networked
            else None
        ),
        resolve_review=(
            ResolveReview(uow_factory, provider, credentials, clock, metric_sink, submit_draft)
            if networked
            else None
        ),
        approve_draft_invoice=ApproveDraftInvoice(uow_factory, clock, metric_sink),
        hold_draft_invoice=HoldDraftInvoice(uow_factory, clock, metric_sink),
        mark_cancelled_externally=MarkCancelledExternally(uow_factory, clock),
        get_document_status=GetDocumentStatus(uow_factory),
        list_open_reviews=ListOpenReviews(uow_factory),
        list_pending_approvals=ListPendingApprovals(uow_factory, clock, settings),
        get_draft_for_approval=GetDraftForApproval(uow_factory),
    )
