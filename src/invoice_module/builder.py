from dataclasses import dataclass

from invoice_module.ports.clock import Clock
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory


@dataclass(frozen=True)
class InvoiceModule:
    uow_factory: InvoiceUnitOfWorkFactory
    clock: Clock
    metrics: MetricsSink


def build_invoice_module(
    uow_factory: InvoiceUnitOfWorkFactory,
    clock: Clock,
    metrics: MetricsSink | None = None,
) -> InvoiceModule:
    return InvoiceModule(uow_factory, clock, metrics or NoOpMetricsSink())
