from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from invoice_module.domain.document import (
    Buyer,
    DraftData,
    InvoiceDocument,
    SettledPayment,
    freeze_document,
)
from invoice_module.domain.enums import BuyerSource, Environment
from invoice_module.domain.errors import InvalidTaxCode, TotalMismatch
from invoice_module.domain.money import LineInput


def account(**over: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "buyer_fallback_name": "TEST-FALLBACK-NAME",
        "buyer_fallback_address": "TEST-FALLBACK-ADDRESS",
        "buyer_fallback_extra": None,
    }
    return SimpleNamespace(**(defaults | over))


def draft(buyer: Buyer | None = None) -> DraftData:
    return DraftData(buyer, (LineInput("Product", "unit", Decimal("1"), 100, 0),), 100)


def test_fallback_buyer_when_missing() -> None:
    assert freeze_document(draft(), account()).buyer_source is BuyerSource.FALLBACK


def test_fallback_extra_fields_merged() -> None:
    frozen = freeze_document(draft(), account(buyer_fallback_extra={"phone": "000"}))
    assert frozen.buyer["phone"] == "000"


def test_fallback_extra_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unsupported buyer fallback fields"):
        freeze_document(draft(), account(buyer_fallback_extra={"tax_code": "000"}))


def test_company_buyer_never_replaced_by_fallback() -> None:
    buyer = Buyer(name="Buyer", tax_code="1234567890", address="Somewhere")
    frozen = freeze_document(draft(buyer), account())
    assert frozen.buyer["name"] == "Buyer"
    assert frozen.buyer["address"] == "Somewhere"
    assert frozen.buyer["tax_code"] == "1234567890"
    assert frozen.buyer_source is BuyerSource.HOST


def test_company_buyer_gets_company_type() -> None:
    buyer = Buyer(name="Buyer", tax_code="1234567890", address="Somewhere")
    assert freeze_document(draft(buyer), account()).buyer["type"] == "company"


@pytest.mark.parametrize(
    "buyer",
    [
        Buyer(tax_code="1234567890", address="A"),
        Buyer(name="Buyer", tax_code="1234567890"),
    ],
)
def test_tax_code_requires_name_and_address(buyer: Buyer) -> None:
    with pytest.raises(ValueError, match="requires a buyer name and address"):
        freeze_document(draft(buyer), account())


def test_individual_type_with_tax_code_rejected() -> None:
    with pytest.raises(ValueError):
        freeze_document(
            draft(Buyer(name="Buyer", tax_code="1234567890", address="A", type="individual")),
            account(),
        )


def test_tax_code_format() -> None:
    for code in ("bad", "123", "1234567890-1234"):
        with pytest.raises(InvalidTaxCode):
            freeze_document(draft(Buyer(name="Buyer", tax_code=code, address="A")), account())


def test_snapshot_json_round_trip_is_canonical() -> None:
    invoice = freeze_document(draft(), account())
    assert InvoiceDocument.from_json(invoice.to_json()).canonical_json() == invoice.canonical_json()


def test_snapshot_from_json_preserves_notes() -> None:
    data = draft()
    with_notes = DraftData(data.buyer, data.lines, data.declared_total_vnd, notes="operator note")
    snapshot = freeze_document(with_notes, account())
    assert InvoiceDocument.from_json(snapshot.to_json()).notes == "operator note"


def test_freeze_document_checks_declared_total() -> None:
    data = draft()
    mismatched = DraftData(data.buyer, data.lines, data.declared_total_vnd + 1)
    with pytest.raises(TotalMismatch) as error:
        freeze_document(mismatched, account())
    assert error.value.computed_total_vnd == data.declared_total_vnd


def test_snapshot_keeps_line_name_and_unit() -> None:
    invoice = freeze_document(draft(), account())
    assert (invoice.lines[0]["name"], invoice.lines[0]["unit"]) == ("Product", "unit")


def test_settled_payment_requires_aware_time() -> None:
    with pytest.raises(ValueError):
        SettledPayment(
            uuid4(),
            "tenant",
            uuid4(),
            Environment.TEST,
            uuid4(),
            uuid4(),
            uuid4(),
            10,
            datetime(2026, 1, 1),
            "auto",
            "order",
            "1",
        )


@pytest.mark.parametrize("amount", [0, -1, True])
def test_settled_payment_requires_positive_non_boolean_amount(amount: int | bool) -> None:
    with pytest.raises(ValueError):
        SettledPayment(
            uuid4(),
            "tenant",
            uuid4(),
            Environment.TEST,
            uuid4(),
            uuid4(),
            uuid4(),
            amount,
            datetime.now().astimezone(),
            "auto",
            "order",
            "1",
        )
