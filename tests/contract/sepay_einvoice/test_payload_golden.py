import dataclasses
import inspect
import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from invoice_module.adapters.sepay import endpoints
from invoice_module.adapters.sepay.payload import build_create_payload
from invoice_module.domain.document import Buyer, DraftData, InvoiceDocument, freeze_document
from invoice_module.domain.enums import BuyerSource
from invoice_module.domain.money import LineInput
from invoice_module.ports.provider import IssuerAccountView
from tools.einvoice_probe.scenarios import _invoice_payload

pytestmark = pytest.mark.contract

GOLDEN = Path(__file__).parent / "golden"
REFERENCE = "00000000-0000-4000-8000-00000000c0de"
ISSUED = date(2026, 10, 20)

# Request fields documented by the create API (docs xuat-hoa-don-dien-tu, OpenAPI schemas
# CreateInvoiceRequest, Buyer and InvoiceItem, read 2026-10-20).
DOCUMENTED_REQUEST = {
    "template_code", "invoice_series", "issued_date", "currency", "provider_account_id",
    "seller_store_xid", "reference_code", "payment_method", "is_draft", "buyer", "items",
    "notes", "total_amount",
}  # fmt: skip
DOCUMENTED_BUYER = {
    "type", "name", "legal_name", "tax_code", "address", "email", "phone", "buyer_code",
    "national_id",
}  # fmt: skip
DOCUMENTED_ITEM = {
    "line_number", "line_type", "item_code", "item_name", "unit", "quantity", "unit_price",
    "tax_rate", "tax_amount", "discount_tax", "discount_amount",
    "before_discount_and_tax_amount",
}  # fmt: skip


def golden(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def roundtrip(payload: dict[str, object]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def test_create_payload_golden_host_buyer(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    payload = build_create_payload(account, host_document, REFERENCE, ISSUED)
    assert roundtrip(payload) == golden("create_host_buyer.json")
    assert payload["buyer"]["type"] == "company"
    assert payload["buyer"]["tax_code"] == "0000000000"
    assert set(payload) <= DOCUMENTED_REQUEST
    assert set(payload["buyer"]) <= DOCUMENTED_BUYER
    assert all(set(item) <= DOCUMENTED_ITEM for item in payload["items"])


def test_payload_field_names_match_recorded_sandbox_calls(
    account: IssuerAccountView,
    host_document: InvoiceDocument,
    recorded: Callable[[str], dict[str, Any]],
) -> None:
    payload = build_create_payload(account, host_document, REFERENCE, ISSUED)
    probe = _invoice_payload(
        REFERENCE,
        10,
        "2026-10-20 00:00:00",
        {"provider_account_id": "p", "template_code": "1", "invoice_series": "C26TSE"},
        is_draft=False,
    )
    assert set(probe) <= set(payload)
    assert set(probe["items"][0]) <= set(payload["items"][0])
    lookup_item = recorded("get_by_reference.json")["body"]["data"]["items"][0]
    assert set(lookup_item) - {"total_amount"} <= set(payload["items"][0])


def test_create_payload_golden_fallback_buyer(
    account: IssuerAccountView, fallback_document: InvoiceDocument
) -> None:
    payload = build_create_payload(account, fallback_document, REFERENCE, ISSUED)
    assert roundtrip(payload) == golden("create_fallback_buyer.json")
    assert payload["buyer"] == {"name": "Người mua không lấy hóa đơn"}


def test_create_payload_keeps_line_name_unit_and_code(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    items = build_create_payload(account, host_document, REFERENCE, ISSUED)["items"]
    assert [item["item_name"] for item in items] == ["Phần mềm mẫu", "Dịch vụ cài đặt"]
    assert [item["unit"] for item in items] == ["Tháng", "Lần"]
    assert items[0]["item_code"] == "SYN-001"
    assert [item["line_number"] for item in items] == [1, 2]
    assert items[1] == {
        "before_discount_and_tax_amount": 100000,
        "discount_amount": 10000,
        "item_name": "Dịch vụ cài đặt",
        "line_number": 2,
        "line_type": 1,
        "quantity": 2,
        "tax_amount": 7200,
        "tax_rate": 8,
        "unit": "Lần",
        "unit_price": 50000,
    }


def test_line_without_item_code_is_sent_without_one(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    items = build_create_payload(account, host_document, REFERENCE, ISSUED)["items"]
    assert host_document.lines[1]["item_code"] is None
    assert "item_code" not in items[1]


def test_create_payload_is_deterministic(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    first = build_create_payload(account, host_document, REFERENCE, ISSUED)
    second = build_create_payload(account, host_document, REFERENCE, ISSUED)
    assert first == second
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    assert list(first) == sorted(first)
    assert list(first["buyer"]) == sorted(first["buyer"])
    assert all(list(item) == sorted(item) for item in first["items"])


@pytest.mark.parametrize(
    ("issued", "series"),
    [(date(2026, 10, 20), "C26TSE"), (date(2027, 1, 1), "C27TSE"), (date(2009, 5, 5), "C09TSE")],
)
def test_series_template_expands_year(
    account: IssuerAccountView, host_document: InvoiceDocument, issued: date, series: str
) -> None:
    payload = build_create_payload(account, host_document, REFERENCE, issued)
    assert payload["invoice_series"] == series
    assert payload["issued_date"] == f"{issued.isoformat()} 00:00:00"


def test_total_amount_equals_document_total(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    payload = build_create_payload(account, host_document, REFERENCE, ISSUED)
    assert payload["total_amount"] == host_document.total_vnd == 491880
    assert type(payload["total_amount"]) is int


def test_create_payload_always_sends_is_draft_false(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    payload = build_create_payload(account, host_document, REFERENCE, ISSUED)
    assert payload["is_draft"] is False
    assert list(inspect.signature(build_create_payload).parameters) == [
        "account",
        "doc",
        "reference_code",
        "issued_date",
    ]


def test_fallback_buyer_payload_omits_type(
    account: IssuerAccountView, fallback_document: InvoiceDocument
) -> None:
    payload = build_create_payload(account, fallback_document, REFERENCE, ISSUED)
    assert "type" not in fallback_document.buyer
    assert "type" not in payload["buyer"]


def test_fallback_buyer_blank_address_follows_probe_rule(
    account: IssuerAccountView,
    fallback_document: InvoiceDocument,
    recorded: Callable[[str], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert recorded("findings_buyer.json")["address_rule"] == endpoints.BLANK_ADDRESS_RULE
    assert endpoints.BLANK_ADDRESS_RULE == "omit"
    assert fallback_document.buyer["address"] == ""
    assert (
        "address"
        not in build_create_payload(account, fallback_document, REFERENCE, ISSUED)["buyer"]
    )
    spaced = dataclasses.replace(fallback_document, buyer={"name": "X", "address": "   "})
    assert "address" not in build_create_payload(account, spaced, REFERENCE, ISSUED)["buyer"]
    filled = dataclasses.replace(fallback_document, buyer={"name": "X", "address": " 1 Phố A "})
    assert build_create_payload(account, filled, REFERENCE, ISSUED)["buyer"]["address"] == (
        " 1 Phố A "
    )
    monkeypatch.setattr(endpoints, "BLANK_ADDRESS_RULE", "empty_string")
    payload = build_create_payload(account, fallback_document, REFERENCE, ISSUED)
    assert payload["buyer"]["address"] == ""


def test_individual_buyer_type_uses_provider_enum(account: IssuerAccountView) -> None:
    line = LineInput(name="A", unit="B", quantity=Decimal("1"), unit_price_vnd=1000, tax_rate=0)
    buyer = Buyer(name="Người Mẫu", type="individual", phone="0900000000")
    doc = freeze_document(DraftData(buyer, (line,), 1000), account)
    payload = build_create_payload(account, doc, REFERENCE, ISSUED)
    assert payload["buyer"] == {"name": "Người Mẫu", "phone": "0900000000", "type": "personal"}
    assert payload["items"][0]["tax_amount"] == 0


def test_fractional_quantity_is_sent_as_decimal_text(account: IssuerAccountView) -> None:
    line = LineInput(
        name="Giờ công", unit="Giờ", quantity=Decimal("1.50"), unit_price_vnd=1000, tax_rate=0
    )
    doc = freeze_document(DraftData(None, (line,), 1500), account)
    item = build_create_payload(account, doc, REFERENCE, ISSUED)["items"][0]
    assert item["quantity"] == "1.50"
    assert item["before_discount_and_tax_amount"] == 1500


def test_optional_account_and_document_fields(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    payload = build_create_payload(account, host_document, REFERENCE, ISSUED)
    assert "seller_store_xid" not in payload
    assert "notes" not in payload
    with_store = dataclasses.replace(account, seller_store_xid="store-xid")
    with_notes = dataclasses.replace(host_document, notes="Ghi chú")
    payload = build_create_payload(with_store, with_notes, REFERENCE, ISSUED)
    assert payload["seller_store_xid"] == "store-xid"
    assert payload["notes"] == "Ghi chú"


def test_issued_date_must_be_a_date(
    account: IssuerAccountView, host_document: InvoiceDocument
) -> None:
    with pytest.raises(TypeError):
        build_create_payload(account, host_document, REFERENCE, datetime(2026, 10, 20, 9, 0))


# --- money and quantity edge matrix ------------------------------------------------------
# Documents are built directly because ``freeze_document`` already refuses these inputs;
# the adapter keeps its own guard.


def _line(**changes: object) -> dict[str, object]:
    line: dict[str, object] = {
        "name": "Dòng mẫu",
        "unit": "Cái",
        "item_code": "SYN-001",
        "quantity": "1",
        "unit_price_vnd": 1000,
        "discount_vnd": 0,
        "tax_rate": 0,
        "before_vnd": 1000,
        "taxable_vnd": 1000,
        "tax_vnd": 0,
        "line_total_vnd": 1000,
    }
    line.update(changes)
    return line


def _document(line: dict[str, object], total: object = 1000) -> InvoiceDocument:
    return InvoiceDocument(
        buyer={"name": "Người mua mẫu"},
        buyer_source=BuyerSource.FALLBACK,
        lines=(line,),
        total_vnd=total,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(("rate", "tax"), [(-2, 0), (-1, 0), (0, 0), (5, 50), (8, 80), (10, 100)])
def test_tax_rate_matrix_is_sent_unchanged(account: IssuerAccountView, rate: int, tax: int) -> None:
    line = _line(tax_rate=rate, tax_vnd=tax, line_total_vnd=1000 + tax)
    item = build_create_payload(account, _document(line, 1000 + tax), REFERENCE, ISSUED)["items"][0]
    assert item["tax_rate"] == rate
    assert type(item["tax_rate"]) is int
    assert item["tax_amount"] == tax


@pytest.mark.parametrize(
    "field", ["unit_price_vnd", "discount_vnd", "tax_vnd", "before_vnd", "tax_rate"]
)
@pytest.mark.parametrize("value", [1000.0, True, False, Decimal("1000"), "1000", None])
def test_money_fields_refuse_anything_but_int(
    account: IssuerAccountView, field: str, value: object
) -> None:
    with pytest.raises(TypeError, match=field):
        build_create_payload(account, _document(_line(**{field: value})), REFERENCE, ISSUED)


@pytest.mark.parametrize("total", [1000.0, True, Decimal("1000"), "1000"])
def test_document_total_refuses_anything_but_int(account: IssuerAccountView, total: object) -> None:
    with pytest.raises(TypeError, match="total_vnd"):
        build_create_payload(account, _document(_line(), total), REFERENCE, ISSUED)


@pytest.mark.parametrize("quantity", [1.5, 1.0, True])
def test_float_or_bool_quantity_is_refused(account: IssuerAccountView, quantity: object) -> None:
    with pytest.raises(TypeError, match="quantity"):
        build_create_payload(account, _document(_line(quantity=quantity)), REFERENCE, ISSUED)


@pytest.mark.parametrize(
    "quantity", ["NaN", "sNaN", "Infinity", "-Infinity", Decimal("NaN"), Decimal("Infinity")]
)
def test_non_finite_quantity_is_refused(account: IssuerAccountView, quantity: object) -> None:
    with pytest.raises(ValueError, match="quantity"):
        build_create_payload(account, _document(_line(quantity=quantity)), REFERENCE, ISSUED)


@pytest.mark.parametrize(
    ("quantity", "sent"),
    [
        ("1.50", "1.50"),
        (Decimal("1.5"), "1.5"),
        ("0.01", "0.01"),
        ("2", 2),
        ("2.00", 2),
        (Decimal("3"), 3),
        (4, 4),
    ],
)
def test_quantity_wire_form(account: IssuerAccountView, quantity: object, sent: object) -> None:
    item = build_create_payload(account, _document(_line(quantity=quantity)), REFERENCE, ISSUED)
    assert item["items"][0]["quantity"] == sent
    assert type(item["items"][0]["quantity"]) is type(sent)
