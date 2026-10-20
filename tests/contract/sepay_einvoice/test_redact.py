"""The log redactor; every value below is synthetic."""

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from invoice_module.adapters.sepay.redact import normalise_key, redact
from invoice_module.ports.provider import IssuerAccountView

pytestmark = pytest.mark.contract

TAX = "0000000000"
PII = "REDACTED"
REFERENCE = "00000000-0000-4000-8000-00000000c0de"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("buyerTaxCode", "buyer_tax_code"),
        ("BuyerTaxCode", "buyer_tax_code"),
        ("buyer_tax_code", "buyer_tax_code"),
        ("buyerNationalId", "buyer_national_id"),
        ("XMLUrl", "xml_url"),
    ],
)
def test_normalise_key(key: str, expected: str) -> None:
    assert normalise_key(key) == expected


@pytest.mark.parametrize("key", ["buyer_tax_code", "buyerTaxCode", "BuyerTaxCode"])
def test_redact_masks_flat_buyer_keys_any_case(key: str) -> None:
    assert redact({key: "0312345678"}) == {key: TAX}
    assert redact({"buyer": {"taxCode": "0312345678"}}) == {"buyer": {"taxCode": TAX}}
    assert redact({"buyer": {"tax_code": "0312345678-001"}}) == {"buyer": {"tax_code": TAX}}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("buyer_name", "Synthetic Person"),
        ("buyerName", "Synthetic Person"),
        ("buyer_phone", "0900000001"),
        ("buyerEmail", "someone@example.com"),
        ("buyer_address", "1 Synthetic Street"),
        ("BuyerLegalName", "SYNTHETIC CO"),
        ("buyer_phone", 900000001),
        ("buyer_address", {"street": "1 Synthetic Street"}),
        ("buyerBankAccount", ["0001", "0002"]),
    ],
)
def test_redact_masks_flat_buyer_pii_keys(key: str, value: Any) -> None:
    assert redact({key: value}) == {key: PII}


def test_redact_masks_nested_buyer_mapping_and_keeps_other_fields() -> None:
    body = {
        "data": {
            "buyer": {
                "name": "Synthetic Person",
                "legalName": "SYNTHETIC CO",
                "phone": "0900000001",
                "email": "someone@example.com",
                "buyer_code": "KH-001",
                "type": "personal",
            },
            "items": [{"item_name": "Synthetic item", "unit": "lần"}],
            "invoice_number": "100001",
        }
    }
    redacted = redact(body)
    assert redacted["data"]["buyer"] == {
        "name": PII,
        "legalName": PII,
        "phone": PII,
        "email": PII,
        "buyer_code": "KH-001",
        "type": "personal",
    }
    assert redacted["data"]["items"] == body["data"]["items"]
    assert redacted["data"]["invoice_number"] == "100001"
    assert body["data"]["buyer"]["name"] == "Synthetic Person"


@pytest.mark.parametrize(
    ("path", "mask"),
    [
        ("buyer.taxCode", TAX),
        ("buyer.tax_code", TAX),
        ("body.data.buyer.TaxCode", TAX),
        ("buyerName", PII),
        ("data.buyer.phoneNumber", PII),
        ("body.data.invoice.buyer.address", PII),
    ],
)
def test_redact_masks_descriptor_paths_with_normalised_tail(path: str, mask: str) -> None:
    assert redact({"path": path, "value": "synthetic value"}) == {"path": path, "value": mask}
    nested = redact({"details": [{"path": path, "value": "synthetic value"}]})
    assert nested == {"details": [{"path": path, "value": mask}]}


@pytest.mark.parametrize(
    "path",
    ["items.0.name", "data.invoice.status", "seller.name", "data.sellerName", "seller_tax_code"],
)
def test_redact_keeps_descriptor_paths_outside_buyer(path: str) -> None:
    descriptor = {"path": path, "value": "kept"}
    assert redact(descriptor) == descriptor
    assert redact({"seller_tax_code": "0100000000", "sellerName": "Issuer"}) == {
        "seller_tax_code": "0100000000",
        "sellerName": "Issuer",
    }


def test_redact_keeps_blank_or_null_buyer_address() -> None:
    assert redact({"buyer_address": ""}) == {"buyer_address": ""}
    assert redact({"buyer": {"address": None}}) == {"buyer": {"address": None}}
    assert redact({"buyerAddress": None}) == {"buyerAddress": None}
    assert redact({"path": "body.data.buyer.address", "value": ""})["value"] == ""
    assert redact({"path": "data.buyer.address", "value": None})["value"] is None
    assert redact({"buyer": {"address": "   "}}) == {"buyer": {"address": PII}}
    assert redact({"path": "buyer.address", "value": "  "})["value"] == PII
    assert redact({"buyer_address": "1 Synthetic Street"}) == {"buyer_address": PII}


def _substitute(obj: Any, new: Any) -> Any:
    if isinstance(obj, dict):
        return {key: _substitute(value, new) for key, value in obj.items()}
    return new if obj == "SYNTHETIC-ID" else obj


@pytest.mark.parametrize(
    "body",
    [
        {"buyer_national_id": "SYNTHETIC-ID"},
        {"buyerNationalId": "SYNTHETIC-ID"},
        {"BuyerNationalId": "SYNTHETIC-ID"},
        {"buyer": {"national_id": "SYNTHETIC-ID"}},
        {"buyer": {"nationalId": "SYNTHETIC-ID"}},
        {"path": "data.buyer.nationalId", "value": "SYNTHETIC-ID"},
    ],
)
def test_redact_masks_buyer_national_id_all_forms(
    body: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    redacted = redact(body)
    assert "SYNTHETIC-ID" not in repr(redacted)
    assert PII in repr(redacted)
    for empty in ("", None):
        blank = _substitute(body, empty)
        assert redact(blank) == blank
    caplog.set_level(logging.INFO)
    logging.getLogger("invoice_module.adapters.sepay.client").info("%s", redact(body))
    assert "SYNTHETIC-ID" not in caplog.text


async def test_adapter_logs_pass_through_redactor(
    harness: Callable[..., Any],
    account: IssuerAccountView,
    creds: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="invoice_module")
    body = {
        "success": True,
        "data": {
            "status": "Success",
            "reference_code": REFERENCE,
            "buyerTaxCode": "0399999999",
            "buyer": {"taxCode": "0388888888", "name": "Synthetic Buyer Name"},
            "invoice": {"buyer": {"nationalId": "SYNTHETIC-NATIONAL-ID", "address": ""}},
        },
    }
    h = harness(lambda request: httpx.Response(200, json=body))
    await h.provider.check_create(account, creds, REFERENCE)
    failing = harness(lambda request: httpx.Response(422, json=body))
    with pytest.raises(Exception, match="http_422"):
        await failing.provider.check_create(account, creds, REFERENCE)
    adapter_logs = [r for r in caplog.records if r.name.startswith("invoice_module")]
    assert any(record.levelno == logging.WARNING for record in adapter_logs)
    text = "\n".join(record.getMessage() for record in adapter_logs)
    assert REFERENCE in text
    for value in ("0399999999", "0388888888", "Synthetic Buyer Name", "SYNTHETIC-NATIONAL-ID"):
        assert value not in text
        assert value not in caplog.text
