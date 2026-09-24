class InvoiceError(Exception):
    """Base exception for invoice domain errors."""


class TotalMismatch(InvoiceError):
    def __init__(self, computed_total_vnd: int) -> None:
        self.computed_total_vnd = computed_total_vnd
        super().__init__(f"declared total does not match computed total {computed_total_vnd}")


class TaxAmountOutOfTolerance(InvoiceError):
    def __init__(self, line_index: int) -> None:
        self.line_index = line_index
        super().__init__(f"tax amount is outside tolerance on line {line_index}")


class InvalidTaxRate(InvoiceError):
    pass


class InvalidQuantity(InvoiceError):
    pass


class InvalidTaxCode(InvoiceError):
    pass


class InvalidLine(InvoiceError):
    def __init__(self, line_index: int, field: str) -> None:
        self.line_index = line_index
        self.field = field
        super().__init__(f"invalid {field} on line {line_index}")


class InvalidTransition(InvoiceError):
    def __init__(self, current: object, target: object) -> None:
        self.current, self.target = current, target
        super().__init__(f"invalid transition from {current} to {target}")


class DraftAlreadyConsumed(InvoiceError):
    pass


class DraftNotOpen(InvoiceError):
    pass


class DraftBoundToOtherIntent(InvoiceError):
    pass


class ReferenceStillExists(InvoiceError):
    pass


class ResolutionNotAllowed(InvoiceError):
    pass


class DocumentInFlight(InvoiceError):
    pass


class CorrectionModeLocked(InvoiceError):
    pass


class ScopeMismatch(InvoiceError):
    pass


class UnsupportedEventVersion(InvoiceError):
    pass
