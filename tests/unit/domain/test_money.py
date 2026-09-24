from decimal import Decimal

import pytest

from invoice_module.domain.errors import (
    InvalidLine,
    InvalidQuantity,
    InvalidTaxRate,
    TaxAmountOutOfTolerance,
    TotalMismatch,
)
from invoice_module.domain.money import (
    LineInput,
    check_declared_total,
    compute_line,
    round_half_up,
)


def line(**over: object) -> LineInput:
    values: dict[str, object] = {
        "name": "Subscription",
        "unit": "month",
        "quantity": Decimal("1"),
        "unit_price_vnd": 100,
        "tax_rate": 10,
    }
    return LineInput(**(values | over))  # type: ignore[arg-type]


def test_half_up_rounding_table() -> None:
    assert [round_half_up(Decimal(v)) for v in ("0.5", "1.5", "2.5", "1.49")] == [1, 2, 3, 1]


def test_line_rate_10() -> None:
    assert compute_line(line()).line_total_vnd == 110


def test_line_rate_8() -> None:
    assert compute_line(line(tax_rate=8)).line_total_vnd == 108


def test_line_special_rates_have_zero_tax() -> None:
    for rate in (-2, -1, 0):
        assert compute_line(line(tax_rate=rate)).tax_vnd == 0


def test_quantity_half_unit() -> None:
    assert compute_line(line(quantity=Decimal("0.5"))).before_vnd == 50


def test_discount_before_tax() -> None:
    amounts = compute_line(line(discount_vnd=20))
    assert (amounts.taxable_vnd, amounts.tax_vnd, amounts.line_total_vnd) == (80, 8, 88)


def test_gross_inclusive_price_with_tax_amount_within_tolerance() -> None:
    assert compute_line(line(tax_amount_vnd=9)).line_total_vnd == 109


def test_tax_amount_off_by_two_rejected() -> None:
    with pytest.raises(TaxAmountOutOfTolerance):
        compute_line(line(tax_amount_vnd=12))


def test_negative_tax_amount_rejected_with_line_index() -> None:
    for rate in (-2, -1, 0, 5):
        with pytest.raises(InvalidLine) as error:
            compute_line(line(tax_rate=rate, tax_amount_vnd=-1), line_index=3)
        assert error.value.line_index == 3


def test_invalid_line_reports_actual_line_index() -> None:
    with pytest.raises(InvalidLine) as error:
        compute_line(line(discount_vnd=101), line_index=7)
    assert error.value.line_index == 7


def test_special_rate_only_accepts_zero_tax_amount() -> None:
    for rate in (-2, -1, 0):
        with pytest.raises(InvalidLine):
            compute_line(line(tax_rate=rate, tax_amount_vnd=1))


def test_zero_tax_amount_allowed_for_special_rates() -> None:
    for rate in (-2, -1, 0):
        assert compute_line(line(tax_rate=rate, tax_amount_vnd=0)).tax_vnd == 0


def test_positive_rate_zero_tax_amount_follows_rounding_tolerance() -> None:
    assert compute_line(line(unit_price_vnd=1, tax_rate=5, tax_amount_vnd=0)).tax_vnd == 0
    with pytest.raises(TaxAmountOutOfTolerance):
        compute_line(line(tax_rate=5, tax_amount_vnd=0))


def test_tax_amount_out_of_tolerance_carries_line_index() -> None:
    with pytest.raises(TaxAmountOutOfTolerance) as error:
        compute_line(line(tax_amount_vnd=12), line_index=4)
    assert error.value.line_index == 4


def test_declared_total_mismatch_reports_computed() -> None:
    with pytest.raises(TotalMismatch) as error:
        check_declared_total(100, [line()])
    assert error.value.computed_total_vnd == 110


def test_float_rejected() -> None:
    with pytest.raises(TypeError):
        LineInput("x", "unit", 1.0, 100, 10)  # type: ignore[arg-type]


def test_float_tax_rate_rejected() -> None:
    with pytest.raises(TypeError):
        line(tax_rate=10.0)


@pytest.mark.parametrize("quantity", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_non_finite_quantity_rejected(quantity: Decimal) -> None:
    with pytest.raises(InvalidQuantity):
        line(quantity=quantity)


def test_none_line_name_rejected() -> None:
    with pytest.raises(InvalidLine):
        line(name=None)


def test_quantity_three_decimals_rejected() -> None:
    with pytest.raises(InvalidQuantity):
        line(quantity=Decimal("0.001"))


def test_unknown_tax_rate_rejected() -> None:
    with pytest.raises(InvalidTaxRate):
        line(tax_rate=7)


def test_line_requires_name_and_unit() -> None:
    with pytest.raises(InvalidLine):
        LineInput(" ", "month", Decimal(1), 10, 0)
