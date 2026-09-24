from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from invoice_module.domain.errors import (
    InvalidLine,
    InvalidQuantity,
    InvalidTaxRate,
    TaxAmountOutOfTolerance,
    TotalMismatch,
)

TAX_TOLERANCE_VND = 1
ALLOWED_TAX_RATES = frozenset({-2, -1, 0, 5, 8, 10})


def round_half_up(value: Decimal) -> int:
    if not isinstance(value, Decimal):
        raise TypeError("money calculations require Decimal")
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class LineInput:
    """One validated invoice line.

    The source position is unavailable while this value is constructed, so the
    application boundary that builds a collection of lines must reindex any
    construction-time :class:`InvalidLine` with that source position.
    """

    name: str
    unit: str
    quantity: Decimal
    unit_price_vnd: int
    tax_rate: int
    discount_vnd: int = 0
    tax_amount_vnd: int | None = None
    item_code: str | None = None

    def __post_init__(self) -> None:
        if any(
            isinstance(value, float)
            for value in (
                self.quantity,
                self.unit_price_vnd,
                self.tax_rate,
                self.discount_vnd,
                self.tax_amount_vnd,
            )
        ):
            raise TypeError("float is not supported for money or quantity")
        if not isinstance(self.quantity, Decimal):
            raise TypeError("quantity must be Decimal")
        if not self.quantity.is_finite():
            raise InvalidQuantity("quantity must be finite")
        if not isinstance(self.name, str):
            raise InvalidLine(0, "name")
        if not isinstance(self.unit, str):
            raise InvalidLine(0, "unit")
        name, unit = self.name.strip(), self.unit.strip()
        if not name or len(name) > 500:
            raise InvalidLine(0, "name")
        if not unit or len(unit) > 50:
            raise InvalidLine(0, "unit")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "unit", unit)
        if self.quantity <= 0 or self.quantity.as_tuple().exponent < -2:
            raise InvalidQuantity("quantity must be positive with at most two decimals")
        for value, field in (
            (self.unit_price_vnd, "unit_price_vnd"),
            (self.discount_vnd, "discount_vnd"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidLine(0, field)
        if self.tax_amount_vnd is not None and (
            isinstance(self.tax_amount_vnd, bool) or not isinstance(self.tax_amount_vnd, int)
        ):
            raise InvalidLine(0, "tax_amount_vnd")
        if isinstance(self.tax_rate, bool) or not isinstance(self.tax_rate, int):
            raise InvalidTaxRate(str(self.tax_rate))
        if self.tax_rate not in ALLOWED_TAX_RATES:
            raise InvalidTaxRate(str(self.tax_rate))


@dataclass(frozen=True)
class LineAmounts:
    before_vnd: int
    taxable_vnd: int
    tax_vnd: int
    line_total_vnd: int


def compute_line(line: LineInput, line_index: int = 0) -> LineAmounts:
    try:
        before = round_half_up(line.quantity * Decimal(line.unit_price_vnd))
    except (InvalidOperation, TypeError) as exc:
        raise InvalidQuantity("quantity cannot be calculated") from exc
    taxable = before - line.discount_vnd
    if taxable < 0:
        raise InvalidLine(line_index, "discount_vnd")
    tax = (
        0 if line.tax_rate in {-2, -1, 0} else round_half_up(Decimal(taxable * line.tax_rate) / 100)
    )
    if line.tax_amount_vnd is not None:
        if line.tax_amount_vnd < 0:
            raise InvalidLine(line_index, "tax_amount_vnd")
        if line.tax_rate in {-2, -1, 0} and line.tax_amount_vnd != 0:
            raise InvalidLine(line_index, "tax_amount_vnd")
        if abs(line.tax_amount_vnd - tax) > TAX_TOLERANCE_VND:
            raise TaxAmountOutOfTolerance(line_index)
        tax = line.tax_amount_vnd
    return LineAmounts(before, taxable, tax, taxable + tax)


def compute_total(lines: tuple[LineInput, ...] | list[LineInput]) -> int:
    return sum(compute_line(line, index).line_total_vnd for index, line in enumerate(lines))


def check_declared_total(declared: int, lines: tuple[LineInput, ...] | list[LineInput]) -> int:
    computed = compute_total(lines)
    if declared != computed:
        raise TotalMismatch(computed)
    return computed
