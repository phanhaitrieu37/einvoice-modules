from dataclasses import dataclass

from invoice_module.application import DiscardDraft, RecordSettledPayment, SubmitDraft
from invoice_module.ports.clock import Clock
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory


@dataclass(frozen=True)
class InvoiceModule:
    uow_factory: InvoiceUnitOfWorkFactory
    clock: Clock
    metrics: MetricsSink
    submit_draft: SubmitDraft
    discard_draft: DiscardDraft
    record_settled_payment: RecordSettledPayment


def build_invoice_module(
    uow_factory: InvoiceUnitOfWorkFactory,
    clock: Clock,
    metrics: MetricsSink | None = None,
) -> InvoiceModule:
    metric_sink = metrics or NoOpMetricsSink()
    return InvoiceModule(
        uow_factory,
        clock,
        metric_sink,
        SubmitDraft(uow_factory, clock),
        DiscardDraft(uow_factory),
        RecordSettledPayment(uow_factory, clock, metric_sink),
    )
