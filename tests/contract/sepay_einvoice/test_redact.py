"""The log redactor; every value below is synthetic."""

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from invoice_module.adapters.sepay.redact import normalise_key, redact
from invoice_module.ports.provider import IssuerAccountView, ProviderRejected, ProviderTransient

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


@pytest.mark.parametrize("suffix", ["id_number", "identity_number", "citizen_id", "national_id"])
@pytest.mark.parametrize("form", ["flat", "camel", "nested", "nested_camel", "descriptor"])
def test_redact_masks_every_identity_suffix(suffix: str, form: str) -> None:
    camel = "".join(part.title() for part in suffix.split("_"))
    body = {
        "flat": {f"buyer_{suffix}": "SYNTHETIC-ID"},
        "camel": {f"buyer{camel}": "SYNTHETIC-ID"},
        "nested": {"buyer": {suffix: "SYNTHETIC-ID"}},
        "nested_camel": {"buyer": {camel[0].lower() + camel[1:]: "SYNTHETIC-ID"}},
        "descriptor": {
            "path": f"data.buyer.{camel[0].lower() + camel[1:]}",
            "value": "SYNTHETIC-ID",
        },
    }[form]
    redacted = redact(body)
    assert "SYNTHETIC-ID" not in repr(redacted)
    assert PII in repr(redacted)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"buyer": [{"tax_code": "0312345678"}]}, {"buyer": [{"tax_code": TAX}]}),
        ({"buyer": [{"name": "Synthetic"}, {"x": 1}]}, {"buyer": [{"name": PII}, {"x": 1}]}),
        ({"Buyer": {"taxCode": "0312345678"}}, {"Buyer": {"taxCode": TAX}}),
        ({"BUYER": {"name": "Synthetic"}}, {"BUYER": {"name": PII}}),
        (
            {"path": "data.Buyer.taxCode", "value": "0312345678"},
            {"path": "data.Buyer.taxCode", "value": TAX},
        ),
    ],
)
def test_redact_keeps_buyer_context_through_lists_and_case(body: Any, expected: Any) -> None:
    assert redact(body) == expected


@pytest.mark.parametrize("value", [0, [], {}, False, 0.0])
def test_redact_masks_falsy_values_that_are_not_blank(value: Any) -> None:
    assert redact({"buyer_name": value}) == {"buyer_name": PII}
    assert redact({"buyer": {"tax_code": value}}) == {"buyer": {"tax_code": TAX}}
    assert redact({"path": "buyer.phone", "value": value}) == {"path": "buyer.phone", "value": PII}


LOGGED_VALUES = (
    "Mã số thuế người mua 0000000000 không hợp lệ",
    "0000000000",
    "Synthetic Buyer Name",
    "SYNTHETIC-NATIONAL-ID",
    "JVBERi0xLjQgc3ludGhldGlj",
    "%PDF-1.4",
    "<html",
    "Xuất hóa đơn thất bại",
    "keyinv",
)


async def test_adapter_logs_pass_through_redactor(
    harness: Callable[..., Any],
    account: IssuerAccountView,
    creds: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The adapter logs status, error code, job status and message length, never text.

    Provider messages and bodies stay out of the log (the caller keeps the message);
    downloads, failed jobs, rejected creates and proxy pages are all covered.
    """
    caplog.set_level(logging.DEBUG)
    rejected = {
        "success": False,
        "error": {"code": "VALIDATION_ERROR", "message": LOGGED_VALUES[0]},
    }
    failed_job = {
        "success": True,
        "data": {
            "status": "Failed",
            "reference_code": REFERENCE,
            "message": "Xuất hóa đơn thất bại: Synthetic Buyer Name",
            "invoice": {"buyer": {"tax_code": "0000000000", "nationalId": "SYNTHETIC-NATIONAL-ID"}},
        },
    }
    download = {
        "success": True,
        "data": {
            "file_type": "pdf",
            "file_name": "Synthetic Buyer Name.pdf",
            "content": "JVBERi0xLjQgc3ludGhldGlj",
            "pdf_url": "https://example.test/x?keyinv=1",
        },
    }
    html = "<html><body>Synthetic Buyer Name 0000000000</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, json=rejected)
        if request.url.path.endswith("/download"):
            return httpx.Response(200, json=download)
        if "/check/" in request.url.path:
            return httpx.Response(200, json=failed_job)
        return httpx.Response(502, text=html, headers={"content-type": "text/html"})

    h = harness(handler)
    with pytest.raises(ProviderRejected):
        await h.provider.create(account, creds, {"reference_code": REFERENCE})
    progress = await h.provider.check_create(account, creds, REFERENCE)
    assert progress.provider_message == failed_job["data"]["message"]
    with pytest.raises(ProviderRejected, match="artifact_xml_missing"):
        await h.provider.fetch_artifacts(account, creds, REFERENCE, "unused")
    with pytest.raises(ProviderTransient):
        await h.provider.find_by_reference(account, creds, REFERENCE)
    adapter = [r for r in caplog.records if r.name.startswith("invoice_module")]
    lines = [record.getMessage() for record in adapter]
    text = "\n".join(lines) + caplog.text
    for value in LOGGED_VALUES:
        assert value not in text
    message = len(LOGGED_VALUES[0])
    job = len(failed_job["data"]["message"])
    assert lines == [
        "sepay einvoice POST /v1/invoices/create -> HTTP 400 code=VALIDATION_ERROR status=None"
        f" message_length={message}",
        f"sepay einvoice GET /v1/invoices/create/check/{REFERENCE} -> HTTP 200 code=None"
        f" status=Failed message_length={job}",
        f"sepay einvoice GET /v1/invoices/{REFERENCE}/download?type=pdf -> HTTP 200 code=None"
        " status=None message_length=None",
        f"sepay einvoice GET /v1/invoices/{REFERENCE}/download?type=xml -> HTTP 200 code=None"
        " status=None message_length=None",
        f"sepay einvoice GET /v1/invoices/{REFERENCE} -> HTTP 502 code=None status=None"
        " message_length=None",
    ]
    levels = {record.getMessage().split(" -> ")[0]: record.levelno for record in adapter}
    assert levels["sepay einvoice POST /v1/invoices/create"] == logging.WARNING
    assert levels[f"sepay einvoice GET /v1/invoices/{REFERENCE}"] == logging.WARNING
