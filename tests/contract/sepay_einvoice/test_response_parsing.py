import base64
import copy
import json
import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pytest

from invoice_module.adapters.sepay import endpoints, payload
from invoice_module.adapters.sepay.payload import (
    build_create_payload,
    parse_accepted,
    parse_account_facts,
    parse_invoice,
    parse_progress,
)
from invoice_module.domain.document import InvoiceDocument
from invoice_module.ports.provider import (
    ClientCredentials,
    DocumentExists,
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderConfigError,
    ProviderRejected,
    ProviderTransient,
    QuotaExhausted,
    SendAborted,
)

pytestmark = pytest.mark.contract

RECORDED = Path(__file__).parents[2] / "fixtures" / "sepay_einvoice"
Loader = Callable[[str], dict[str, Any]]
# The reference of the recorded create and check (``create_accepted.json``,
# ``check_success.json``): the tracking code equals the reference.
REFERENCE = "8f3e0182-dcd9-42b5-aed2-6dd5f194f1cb"


def _detail_body(recorded: Loader) -> dict[str, Any]:
    return recorded("provider_accounts.json")["provider_account_details"][0]["body"]


def _lookup(body: dict[str, Any]) -> Any:
    """``parse_invoice`` for the reference the body itself carries."""
    return parse_invoice(body, body["data"]["reference_code"])


# --- parsing recorded and synthetic responses -------------------------------------------


def test_parse_accepted_reads_tracking_code(recorded: Loader) -> None:
    record = recorded("create_accepted.json")
    accepted = parse_accepted(record["body"])
    assert accepted is not None
    assert accepted.tracking_code == record["body"]["data"]["tracking_code"]
    tracking = {"tracking_code": "t-1"}
    for body in (
        {"success": False, "data": tracking},
        {"success": "true", "data": tracking},
        {"data": tracking},
        {"success": True, "data": {"tracking_code": ""}},
        {"success": True, "data": {"tracking_code": 7}},
        {"success": True, "data": {}},
        {"success": True},
        None,
        "<html></html>",
    ):
        assert parse_accepted(body) is None


def test_parse_check_pending(synthetic: Loader) -> None:
    progress = parse_progress(synthetic("check_pending.json")["body"])
    assert progress.state == "pending"
    assert progress.next_retry_at == datetime(
        2026, 10, 20, 15, 12, 30, tzinfo=ZoneInfo("Asia/Saigon")
    )
    assert progress.invoice_number is None
    assert progress.issued_date is None


@pytest.mark.parametrize("value", ["2026-10-23 25:00:00", "2026-02-30 10:00:00", "soon", 5])
def test_parse_check_unreadable_next_retry_at_is_none(synthetic: Loader, value: object) -> None:
    body = copy.deepcopy(synthetic("check_pending.json")["body"])
    body["data"]["retry"]["next_retry_at"] = value
    progress = parse_progress(body)
    assert progress.state == "pending"
    assert progress.next_retry_at is None


@pytest.mark.parametrize("status", ["Error", "failed", "FAILED", "success", "Cancelled", "", None])
def test_parse_check_unknown_status_stays_pending(recorded: Loader, status: object) -> None:
    body = copy.deepcopy(recorded("check_success.json")["body"])
    body["data"]["status"] = status
    body["data"]["message"] = "x"
    progress = parse_progress(body)
    assert progress.state == "pending"
    assert progress.provider_message is None
    assert progress.invoice_number is None


def test_parse_check_success(recorded: Loader) -> None:
    progress = parse_progress(recorded("check_success.json")["body"])
    assert progress.state == "success"
    assert progress.invoice_number == "307613"
    assert progress.issued_date == date(2026, 9, 25)
    assert progress.tax_authority_code is None
    assert progress.error_code is None
    assert progress.provider_message is None


def test_parse_check_failed_if_recorded(recorded: Loader) -> None:
    path = RECORDED / "check_failed.json"
    if path.exists():
        progress = parse_progress(recorded("check_failed.json")["body"])
        assert progress.state == "failed"
    else:
        assert recorded("findings.json")["s3_variant"] != "async_failed"


def test_parse_check_failed_keeps_message_verbatim(synthetic: Loader) -> None:
    record = synthetic("check_failed_localised_message.json")
    assert record["_synthetic"]
    assert record["status"] == 200 and record["body"]["success"] is True
    progress = parse_progress(record["body"])
    assert progress.state == "failed"
    assert progress.provider_message == record["body"]["data"]["message"]
    assert progress.provider_message.startswith("Ngày lập (01/01/2026)")
    assert progress.error_code is None
    assert progress.invoice_number is None


def test_parse_get_by_reference_with_total_when_available(
    recorded: Loader, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert recorded("findings.json")["lookup_returns_total"] is True
    body = recorded("get_by_reference.json")["body"]
    invoice = _lookup(body)
    assert invoice.state == "success"
    assert invoice.total_amount_vnd == 1080
    assert invoice.invoice_number == "307614"
    assert invoice.issued_date == date(2026, 9, 25)
    assert invoice.reference_code == body["data"]["reference_code"]
    monkeypatch.setattr(payload, "LOOKUP_RETURNS_TOTAL", False)
    assert _lookup(body).total_amount_vnd is None


def test_parse_account_facts_and_quota(recorded: Loader, monkeypatch: pytest.MonkeyPatch) -> None:
    quota = recorded("quota.json")["body"]
    facts = parse_account_facts("acc", _detail_body(recorded), quota)
    assert facts.provider_account_id == "acc"
    assert facts.templates == ("1", "2")
    assert facts.series == ("C26TSE", "C26TSE")
    assert facts.quota_remaining == 999
    documented = {"data": {"quota_remaining": "534"}}
    assert parse_account_facts("acc", _detail_body(recorded), documented).quota_remaining == 534
    monkeypatch.setitem(endpoints.ENDPOINTS, "quota", None)
    assert parse_account_facts("acc", _detail_body(recorded), quota).quota_remaining is None


def test_parse_check_draft(recorded: Loader) -> None:
    progress = parse_progress(recorded("draft_check.json")["body"])
    assert progress.state == "draft"
    assert progress.invoice_number is None
    assert progress.issued_date == date(2026, 10, 7)


def test_parse_get_by_reference_draft(recorded: Loader) -> None:
    invoice = _lookup(recorded("draft_lookup.json")["body"])
    assert invoice.state == "draft"
    assert invoice.invoice_number is None


def test_parse_progress_and_invoice_carry_provider_issued_date(recorded: Loader) -> None:
    check = recorded("check_success.json")["body"]
    lookup = recorded("issue_lookup_issued.json")["body"]
    assert parse_progress(check).issued_date == date(2026, 9, 25)
    assert _lookup(lookup).issued_date == date(2026, 10, 7)
    check_without = copy.deepcopy(check)
    del check_without["data"]["invoice"]["issued_date"]
    lookup_without = copy.deepcopy(lookup)
    del lookup_without["data"]["issued_date"]
    assert parse_progress(check_without).issued_date is None
    assert _lookup(lookup_without).issued_date is None
    assert parse_progress(check_without).state == "success"


def test_parse_draft_invoice_number_zero_is_no_number(recorded: Loader) -> None:
    check = recorded("draft_check.json")["body"]
    lookup = recorded("draft_lookup.json")["body"]
    assert check["data"]["invoice"]["invoice_number"] == "0"
    assert lookup["data"]["invoice_number"] == "0"
    assert parse_progress(check).invoice_number is None
    assert parse_progress(check).state == "draft"
    assert _lookup(lookup).invoice_number is None
    assert _lookup(lookup).state == "draft"


def test_parse_invoice_failed_message_present_or_absent(synthetic: Loader) -> None:
    with_message = synthetic("lookup_failed_with_message.json")
    without_message = synthetic("lookup_failed_without_message.json")
    assert with_message["_synthetic"] and without_message["_synthetic"]
    present = _lookup(with_message["body"])
    assert present.state == "failed"
    assert present.provider_message == with_message["body"]["data"]["message"]
    assert present.invoice_number is None
    absent = _lookup(without_message["body"])
    assert absent.state == "failed"
    assert absent.provider_message is None


@pytest.mark.parametrize("status", ["replaced", "Issued", "ISSUED", "Cancelled", "", None, 1])
def test_parse_invoice_unknown_status_is_never_success(recorded: Loader, status: object) -> None:
    body = copy.deepcopy(recorded("get_by_reference.json")["body"])
    body["data"]["status"] = status
    body["data"]["message"] = "x"
    invoice = _lookup(body)
    assert invoice.state == "pending"
    assert invoice.provider_message is None


def test_parse_invoice_cancelled_is_reported_as_cancelled(recorded: Loader) -> None:
    body = copy.deepcopy(recorded("get_by_reference.json")["body"])
    body["data"]["status"] = "cancelled"
    body["data"]["message"] = "x"
    invoice = _lookup(body)
    assert invoice.state == "cancelled"
    assert invoice.invoice_number == "307614"
    assert invoice.provider_message is None


def test_parse_invoice_refuses_another_reference(recorded: Loader) -> None:
    body = recorded("get_by_reference.json")["body"]
    assert body["data"]["reference_code"] != REFERENCE
    with pytest.raises(ProviderRejected) as raised:
        parse_invoice(body, REFERENCE)
    assert raised.value.code == "lookup_reference_mismatch"
    for missing in (None, ""):
        anonymous = copy.deepcopy(body)
        anonymous["data"]["reference_code"] = missing
        assert parse_invoice(anonymous, REFERENCE).reference_code == REFERENCE
    del anonymous["data"]["reference_code"]
    assert parse_invoice(anonymous, REFERENCE).reference_code == REFERENCE


WHITESPACE_MESSAGE = " Xuất hóa đơn thất bại: dữ liệu không hợp lệ \n"


def test_failed_messages_are_kept_with_their_whitespace(synthetic: Loader) -> None:
    check = copy.deepcopy(synthetic("check_failed_localised_message.json")["body"])
    check["data"]["message"] = WHITESPACE_MESSAGE
    assert parse_progress(check).provider_message == WHITESPACE_MESSAGE
    assert parse_progress(check).error_code is None
    lookup = copy.deepcopy(synthetic("lookup_failed_with_message.json")["body"])
    lookup["data"]["message"] = WHITESPACE_MESSAGE
    assert _lookup(lookup).provider_message == WHITESPACE_MESSAGE


def test_tax_authority_code_is_carried_through(recorded: Loader) -> None:
    check = copy.deepcopy(recorded("check_success.json")["body"])
    assert check["data"]["invoice"]["tax_authority_code"] is None
    check["data"]["invoice"]["tax_authority_code"] = "SYN-CQT-1"
    assert parse_progress(check).tax_authority_code == "SYN-CQT-1"
    lookup = copy.deepcopy(recorded("get_by_reference.json")["body"])
    assert lookup["data"]["tax_authority_code"] is None
    lookup["data"]["tax_authority_code"] = "SYN-CQT-2"
    assert _lookup(lookup).tax_authority_code == "SYN-CQT-2"


UNCONFIRMED = "Hệ thống chưa xác nhận được kết quả phát hành với nhà cung cấp hóa đơn"
# The ``message`` of the docs example ``failed_unverified`` (page
# ``theo-doi-trang-thai-xuat-hoa-don``), verbatim.
UNCONFIRMED_DOCS_EXAMPLE = (
    f"{UNCONFIRMED}. Vui lòng không phát hành lại và liên hệ SePay để được kiểm tra."
)


@pytest.mark.parametrize(
    ("status", "message", "error_code"),
    [
        ("Failed", UNCONFIRMED, "provider_unconfirmed"),
        ("Failed", f"  {UNCONFIRMED}\n", "provider_unconfirmed"),
        ("Pending", UNCONFIRMED, "provider_unconfirmed"),
        ("Failed", f"{UNCONFIRMED}.", "provider_unconfirmed"),
        ("Failed", f" {UNCONFIRMED}. Liên hệ SePay. ", "provider_unconfirmed"),
        ("Failed", f"{UNCONFIRMED} đã phát hành", None),
        ("Failed", f"{UNCONFIRMED}, vui lòng chờ", None),
        ("Failed", f"{UNCONFIRMED}x", None),
        ("Failed", f"Lỗi: {UNCONFIRMED}", None),
        ("Failed", UNCONFIRMED.replace("hóa", "hoá"), None),
        ("Failed", UNCONFIRMED.upper(), None),
        ("Failed", "Xuất hóa đơn thất bại", None),
        ("Failed", None, None),
        ("Success", UNCONFIRMED, "provider_unconfirmed"),
        ("Success", None, None),
    ],
)
def test_parse_check_unconfirmed_message_sets_error_code(
    recorded: Loader, status: str, message: str | None, error_code: str | None
) -> None:
    """Docs: this sentence means the outcome is unknown and the request is not resent."""
    body = copy.deepcopy(recorded("check_success.json")["body"])
    body["data"]["status"] = status
    body["data"]["message"] = message
    progress = parse_progress(body)
    assert progress.error_code == error_code
    if status == "Failed":
        assert progress.provider_message == message


def test_parse_check_unconfirmed_docs_example_sets_error_code() -> None:
    """The docs example of a stopped job the provider has not confirmed."""
    body = {
        "success": True,
        "data": {
            "reference_code": REFERENCE,
            "status": "Failed",
            "message": UNCONFIRMED_DOCS_EXAMPLE,
        },
    }
    progress = parse_progress(body)
    assert progress.state == "failed"
    assert progress.error_code == "provider_unconfirmed"
    assert progress.provider_message == UNCONFIRMED_DOCS_EXAMPLE


# --- provider over a replayed transport -------------------------------------------------


def _json(record: dict[str, Any], status: int | None = None) -> httpx.Response:
    return httpx.Response(status or record["status"], json=record["body"])


async def test_provider_create_sends_request_verbatim(
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: ClientCredentials,
    host_document: InvoiceDocument,
) -> None:
    accepted = recorded("create_accepted.json")
    h = harness(lambda request: _json(accepted))
    request = build_create_payload(account, host_document, REFERENCE, date(2026, 10, 20))
    frozen = json.loads(json.dumps(request))
    result = await h.provider.create(account, creds, frozen)
    creates = h.calls("/v1/invoices/create", "POST")
    assert len(creates) == 1
    assert json.loads(creates[0].content) == frozen
    assert creates[0].url.host == "einvoice-api-sandbox.sepay.vn"
    assert creates[0].headers["Authorization"].startswith("Bearer synthetic-access-token-value")
    assert result.tracking_code == accepted["body"]["data"]["tracking_code"]


async def test_provider_create_then_check_success(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    accepted, success = recorded("create_accepted.json"), recorded("check_success.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return _json(accepted) if request.method == "POST" else _json(success)

    h = harness(handler)
    tracking = (
        await h.provider.create(account, creds, {"reference_code": REFERENCE})
    ).tracking_code
    progress = await h.provider.check_create(account, creds, tracking)
    assert h.calls("/check/")[0].url.path == f"/v1/invoices/create/check/{tracking}"
    assert progress.state == "success"
    assert progress.invoice_number == "307613"
    assert progress.issued_date == date(2026, 9, 25)
    assert h.events.count("token") == 1


async def test_provider_create_409_raises_document_exists(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    h = harness(lambda request: _json(recorded("create_409.json")))
    with pytest.raises(DocumentExists):
        await h.provider.create(account, creds, {"reference_code": REFERENCE})
    assert len(h.calls("/v1/invoices/create", "POST")) == 1


def test_recorded_unknown_reference_fixture_is_404_with_envelope(recorded: Loader) -> None:
    record = recorded("lookup_unknown_reference.json")
    assert record["status"] == 404
    assert record["request"] == {
        "method": "GET",
        "path": endpoints.ENDPOINTS["get_by_reference"][1],
    }
    assert record["headers"]["content-type"].startswith("application/json")
    assert record["body"]["success"] is False
    assert record["body"]["error"]["code"] == "EINVOICE_DOCUMENT_NOT_FOUND"
    assert isinstance(record["body"]["error"]["message"], str)
    assert "data" not in record["body"]


async def test_provider_find_by_reference_none_on_404(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    """Replays the sandbox answer for a reference it never saw."""
    h = harness(lambda request: _json(recorded("lookup_unknown_reference.json")))
    assert await h.provider.find_by_reference(account, creds, REFERENCE) is None
    documented = {"success": False, "error": {"code": "NOT_FOUND", "message": "x"}}
    docs = harness(lambda request: httpx.Response(404, json=documented))
    assert await docs.provider.find_by_reference(account, creds, REFERENCE) is None
    assert [r.url.path for r in h.calls("/v1/invoices/")] == [f"/v1/invoices/{REFERENCE}"]
    failing = harness(lambda request: httpx.Response(500, json={}))
    with pytest.raises(ProviderTransient):
        await failing.provider.find_by_reference(account, creds, REFERENCE)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, text="<html>Not Found</html>", headers={"content-type": "text/html"}),
        httpx.Response(404),
        httpx.Response(404, json={"foo": 1}),
        httpx.Response(404, json={"success": False}),
        httpx.Response(404, json={"success": False, "error": {"code": ""}}),
        httpx.Response(404, json={"success": True, "error": {"code": "NOT_FOUND"}}),
        httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}),
        httpx.Response(404, json=[{"success": False, "error": {"code": "NOT_FOUND"}}]),
    ],
)
async def test_lookup_404_without_error_envelope_is_transient(
    response: httpx.Response, harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    h = harness(lambda request: response)
    with pytest.raises(ProviderTransient) as raised:
        await h.provider.find_by_reference(account, creds, REFERENCE)
    assert raised.value.retry_after is None
    assert len(h.calls(f"/v1/invoices/{REFERENCE}")) == 1


async def test_provider_find_by_reference_missing_numbers_returns_incomplete(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    body = copy.deepcopy(recorded("get_by_reference.json")["body"])
    body["data"]["invoice_number"] = None
    del body["data"]["issued_date"]
    reference = body["data"]["reference_code"]
    h = harness(lambda request: httpx.Response(200, json=body))
    invoice = await h.provider.find_by_reference(account, creds, reference)
    assert invoice is not None
    assert invoice.invoice_number is None
    assert invoice.issued_date is None
    assert invoice.total_amount_vnd == 1080
    assert [r.url.path for r in h.requests if r.url.path != "/v1/token"] == [
        f"/v1/invoices/{reference}"
    ]


async def test_provider_fetch_artifacts_returns_pdf_and_xml(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    assert recorded("findings.json")["download_returns"] == "bytes"
    files = {"pdf": b"%PDF-1.4 synthetic", "xml": b"<HDon>synthetic</HDon>"}

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.params["type"]
        content = base64.b64encode(files[kind]).decode()
        data = {"file_type": kind, "file_name": f"x.{kind}", "content": content}
        return httpx.Response(200, json={"success": True, "data": data})

    h = harness(handler)
    artifacts = await h.provider.fetch_artifacts(account, creds, REFERENCE, "unused")
    assert artifacts.pdf == files["pdf"]
    assert artifacts.xml == files["xml"]
    downloads = h.calls("/download")
    assert [r.url.path for r in downloads] == [f"/v1/invoices/{REFERENCE}/download"] * 2
    assert [r.url.params["type"] for r in downloads] == ["pdf", "xml"]
    for content in ("not base64!", "SGVsbG8=?!", "", "\n", "\r\n"):
        bad = harness(lambda request, c=content: httpx.Response(200, json={"data": {"content": c}}))
        with pytest.raises(ProviderRejected):
            await bad.provider.fetch_artifacts(account, creds, REFERENCE, "unused")
    pdf = base64.b64encode(files["pdf"]).decode()
    wrapped = harness(
        lambda request: httpx.Response(
            200, json={"data": {"content": pdf[:8] + "\r\n" + pdf[8:] + "\n"}}
        )
    )
    assert (await wrapped.provider.fetch_artifacts(account, creds, REFERENCE, "u")).pdf == files[
        "pdf"
    ]
    for content, code in (("PEhEb24+?!", "artifact_xml_invalid"), ("", "artifact_xml_missing")):

        def strict(request: httpx.Request, c: str = content) -> httpx.Response:
            data = c if request.url.params["type"] == "xml" else pdf
            return httpx.Response(200, json={"data": {"content": data}})

        with pytest.raises(ProviderRejected) as raised:
            await harness(strict).provider.fetch_artifacts(account, creds, REFERENCE, "unused")
        assert raised.value.code == code


async def test_provider_describe_account(
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail, quota = _detail_body(recorded), recorded("quota.json")["body"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=quota if request.url.path == "/v1/usage" else detail)

    h = harness(handler)
    facts = await h.provider.describe_account(account, creds)
    assert facts.templates == ("1", "2")
    assert facts.series == ("C26TSE", "C26TSE")
    assert facts.quota_remaining == 999
    assert h.calls("/v1/provider-accounts/")[0].url.path == (
        f"/v1/provider-accounts/{account.provider_account_id}"
    )
    monkeypatch.setitem(endpoints.ENDPOINTS, "quota", None)
    h = harness(handler)
    assert (await h.provider.describe_account(account, creds)).quota_remaining is None
    assert h.calls("/v1/usage") == []


async def test_provider_lists_active_provider_accounts(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView
) -> None:
    listing = copy.deepcopy(recorded("provider_accounts.json")["body"])
    inactive = {"active": False, "id": "00000000-0000-0000-0000-0000000000ff", "provider": "x"}
    listing["data"]["items"].append(inactive)
    h = harness(lambda request: httpx.Response(200, json=listing))
    assert await h.provider.provider_account_ids(account) == (
        "00000000-0000-0000-0000-000000000000",
    )
    assert h.calls("/v1/provider-accounts")[0].url.path == "/v1/provider-accounts"


async def test_provider_before_send_runs_after_token_and_limiter_and_can_abort(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    h = harness(lambda request: _json(recorded("create_accepted.json")))

    async def allow() -> bool:
        h.events.append("hook")
        return True

    await h.provider.create(account, creds, {"reference_code": REFERENCE}, before_send=allow)
    assert h.events[h.events.index("token") :] == [
        "token",
        "limiter",
        "hook",
        "POST /v1/invoices/create",
    ]

    aborted = harness(lambda request: _json(recorded("create_accepted.json")))

    async def refuse() -> bool:
        aborted.events.append("hook")
        return False

    with pytest.raises(SendAborted):
        await aborted.provider.create(
            account, creds, {"reference_code": REFERENCE}, before_send=refuse
        )
    assert aborted.events[-3:] == ["token", "limiter", "hook"]
    assert aborted.calls("/v1/invoices/create") == []


def _raise(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


CREATE_FAILURES = {
    "401_cached_token": (lambda r: _unauthorized(), ProviderTransient, True),
    "401_fresh_token": (lambda r: _unauthorized(), ProviderAuthFailed, False),
    "403_cached_token": (lambda r: _forbidden(), ProviderAuthFailed, True),
    "403_fresh_token": (lambda r: _forbidden(), ProviderAuthFailed, False),
    "502_html": (lambda r: _html(502), ProviderTransient, False),
    "200_html": (lambda r: _html(200), ProviderTransient, False),
    "deeply_nested_json": (lambda r: _nested(), ProviderTransient, False),
    "307": (lambda r: _redirect(307), ProviderTransient, False),
    "308": (lambda r: _redirect(308), ProviderTransient, False),
    "timeout": (_raise(httpx.ReadTimeout("slow")), ProviderTransient, False),
    "5xx": (lambda r: httpx.Response(503, json={}), ProviderTransient, False),
    "connect_error": (_raise(httpx.ConnectError("down")), ProviderTransient, False),
    "202_corrupt_gzip": (lambda r: _corrupt_gzip(202), ProviderTransient, False),
    "500_error_as_text": (
        lambda r: httpx.Response(500, json={"error": "boom"}),
        ProviderTransient,
        False,
    ),
}


def _corrupt_gzip(status: int) -> httpx.Response:
    return httpx.Response(status, headers={"content-encoding": "gzip"}, content=b"not gzip")


def _unauthorized() -> httpx.Response:
    return httpx.Response(401, json={"success": False, "error": {"code": "UNAUTHORIZED"}})


def _forbidden() -> httpx.Response:
    return httpx.Response(403, json={"success": False, "error": {"code": "FORBIDDEN"}})


def _html(status: int) -> httpx.Response:
    page = "<html><body>Bad Gateway</body></html>"
    return httpx.Response(status, text=page, headers={"content-type": "text/html"})


def _nested(status: int = 200) -> httpx.Response:
    content = b"[" * 200000
    return httpx.Response(status, content=content, headers={"content-type": "application/json"})


def _redirect(status: int) -> httpx.Response:
    location = "https://einvoice-api-sandbox.sepay.vn/v1/invoices/create"
    return httpx.Response(status, headers={"Location": location})


@pytest.mark.parametrize("case", sorted(CREATE_FAILURES))
async def test_provider_create_calls_before_send_once_and_never_retries(
    case: str,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    create_handler, error, warm_token = CREATE_FAILURES[case]
    success = recorded("check_success.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return create_handler(request)
        return _json(success)

    h = harness(handler)
    if warm_token:
        await h.provider.check_create(account, creds, REFERENCE)
    hooks = 0

    async def hook() -> bool:
        nonlocal hooks
        hooks += 1
        return True

    with pytest.raises(error):
        await h.provider.create(account, creds, {"reference_code": REFERENCE}, before_send=hook)
    assert hooks == 1
    assert len(h.calls("/v1/invoices/create", "POST")) == 1
    if case.startswith("401"):
        tokens_before = h.events.count("token")
        await h.provider.check_create(account, creds, REFERENCE)
        assert h.events.count("token") == tokens_before + 1
        assert len(h.calls("/v1/invoices/create", "POST")) == 1


NOT_ACCEPTED = {
    "200_success_false_with_code": (
        lambda: httpx.Response(200, json={"success": False, "error": {"code": "VALIDATION_ERROR"}}),
        ProviderRejected,
        "VALIDATION_ERROR",
    ),
    "202_success_false_issued_date": (
        lambda: httpx.Response(
            202, json={"success": False, "error": {"code": "ISSUED_DATE_INVALID"}}
        ),
        ProviderRejected,
        "ISSUED_DATE_INVALID",
    ),
    "200_success_true_issued_date": (
        lambda: httpx.Response(
            200, json={"success": True, "error": {"code": "ISSUED_DATE_INVALID"}}
        ),
        ProviderRejected,
        "ISSUED_DATE_INVALID",
    ),
    "200_quota_code": (
        lambda: httpx.Response(
            200, json={"success": False, "error": {"code": "QUOTA_HAS_BEEN_USERD_UP"}}
        ),
        QuotaExhausted,
        "QUOTA_HAS_BEEN_USERD_UP",
    ),
    "200_html": (lambda: _html(200), ProviderTransient, None),
    "200_empty": (lambda: httpx.Response(200), ProviderTransient, None),
    "204": (lambda: httpx.Response(204), ProviderTransient, None),
    "200_success_false_without_code": (
        lambda: httpx.Response(200, json={"success": False}),
        ProviderTransient,
        None,
    ),
    "202_success_true_without_tracking": (
        lambda: httpx.Response(202, json={"success": True, "data": {"message": "x"}}),
        ProviderTransient,
        None,
    ),
    "202_success_true_data_list": (
        lambda: httpx.Response(202, json={"success": True, "data": ["x"]}),
        ProviderTransient,
        None,
    ),
    "202_success_true_data_text": (
        lambda: httpx.Response(202, json={"success": True, "data": "x"}),
        ProviderTransient,
        None,
    ),
    "202_tracking_without_success": (
        lambda: httpx.Response(202, json={"data": {"tracking_code": "t-1"}}),
        ProviderTransient,
        None,
    ),
}


@pytest.mark.parametrize("case", sorted(NOT_ACCEPTED))
async def test_provider_create_2xx_without_acceptance_is_not_accepted(
    case: str, harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    """Docs: only ``success: true`` with ``data.tracking_code`` means the request was taken."""
    response, error, code = NOT_ACCEPTED[case]
    h = harness(lambda request: response())
    hooks = 0

    async def hook() -> bool:
        nonlocal hooks
        hooks += 1
        return True

    with pytest.raises(error) as raised:
        await h.provider.create(account, creds, {"reference_code": REFERENCE}, before_send=hook)
    assert type(raised.value) is error
    if code is not None:
        assert str(raised.value) == code
    else:
        assert raised.value.retry_after is None
    assert hooks == 1
    assert len(h.calls("/v1/invoices/create", "POST")) == 1


@pytest.mark.parametrize("status", [200, 202])
async def test_provider_create_accepts_success_with_tracking_code(
    status: int,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    body = recorded("create_accepted.json")["body"]
    h = harness(lambda request: httpx.Response(status, json=body))
    accepted = await h.provider.create(account, creds, {"reference_code": REFERENCE})
    assert accepted.tracking_code == body["data"]["tracking_code"]


@pytest.mark.parametrize("tracking", ["other-ref", " ", f"{REFERENCE} ", REFERENCE.upper()])
async def test_create_tracking_code_other_than_the_reference_is_transient(
    tracking: str, harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    """Docs: the tracking code is also the reference code of the new invoice."""
    body = {"success": True, "data": {"tracking_code": tracking}}
    h = harness(lambda request: httpx.Response(202, json=body))
    hooks = 0

    async def hook() -> bool:
        nonlocal hooks
        hooks += 1
        return True

    with pytest.raises(ProviderTransient) as raised:
        await h.provider.create(account, creds, {"reference_code": REFERENCE}, before_send=hook)
    assert raised.value.retry_after is None
    assert hooks == 1
    assert len(h.calls("/v1/invoices/create", "POST")) == 1


@pytest.mark.parametrize("returned", ["other-ref", " ", "", 5, f"{REFERENCE} "])
async def test_check_of_another_reference_is_transient(
    returned: object,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    body = copy.deepcopy(recorded("check_success.json")["body"])
    body["data"]["reference_code"] = returned
    h = harness(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ProviderTransient):
        await h.provider.check_create(account, creds, REFERENCE)
    del body["data"]["reference_code"]
    assert (await h.provider.check_create(account, creds, REFERENCE)).state == "success"


@pytest.mark.parametrize("call", ["check", "lookup"])
async def test_success_false_on_check_or_lookup_is_transient(
    call: str,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    """An error envelope in a 200 is not a job or an invoice, whatever its ``data`` says."""
    name = "check_success.json" if call == "check" else "get_by_reference.json"
    body = copy.deepcopy(recorded(name)["body"])
    reference = body["data"]["reference_code"]
    body["success"] = False
    body["error"] = {"code": "INTERNAL_SERVER_ERROR", "message": "x"}
    h = harness(lambda request: httpx.Response(200, json=body))
    read = h.provider.check_create if call == "check" else h.provider.find_by_reference
    with pytest.raises(ProviderTransient):
        await read(account, creds, reference)
    body["success"] = True
    assert (await read(account, creds, reference)).state == "success"


def _with_number(body: Any, path: str, number: str) -> bytes:
    """``body`` as JSON bytes with the raw JSON token ``number`` at the dotted ``path``."""
    body = copy.deepcopy(body)
    *parents, key = path.split(".")
    node = body
    for parent in parents:
        node = node[parent]
    node[key] = "__NUMBER__"
    return json.dumps(body).replace('"__NUMBER__"', number).encode()


def _number_calls(recorded: Loader) -> dict[str, tuple[dict[str, Any], str]]:
    return {
        "create": (
            {"success": True, "data": {"tracking_code": REFERENCE, "total_amount": 0}},
            "data.total_amount",
        ),
        "check": (recorded("check_success.json")["body"], "data.invoice.total_amount"),
        "lookup": (recorded("get_by_reference.json")["body"], "data.total_amount"),
        "usage": (recorded("quota.json")["body"], "data.quota_remaining"),
    }


async def _call_with_number(
    call: str, number: str, recorded: Loader, harness: Callable[..., Any], account: Any, creds: Any
) -> Any:
    body, path = _number_calls(recorded)[call]
    raw = _with_number(body, path, number)
    detail = _detail_body(recorded)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/provider-accounts/"):
            return httpx.Response(200, json=detail)
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    h = harness(handler)
    if call == "create":
        return await h.provider.create(account, creds, {"reference_code": REFERENCE})
    if call == "check":
        return await h.provider.check_create(account, creds, REFERENCE)
    if call == "lookup":
        reference = recorded("get_by_reference.json")["body"]["data"]["reference_code"]
        return await h.provider.find_by_reference(account, creds, reference)
    return await h.provider.describe_account(account, creds)


@pytest.mark.parametrize(
    "number",
    ["NaN", "Infinity", "-Infinity", "1e1000000", "1e999999999999999999", "1e9999999999999999999"],
)
@pytest.mark.parametrize("call", ["create", "check", "lookup", "usage"])
async def test_non_finite_or_huge_json_number_is_transient_and_fast(
    call: str,
    number: str,
    recorded: Loader,
    harness: Callable[..., Any],
    account: IssuerAccountView,
    creds: Any,
) -> None:
    started = time.perf_counter()
    with pytest.raises(ProviderTransient) as raised:
        await _call_with_number(call, number, recorded, harness, account, creds)
    assert time.perf_counter() - started < 2
    assert raised.value.retry_after is None
    assert raised.value.__cause__ is None and raised.value.__context__ is None


async def test_json_numbers_are_read_as_decimal_never_float(
    recorded: Loader, harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    """A JSON fraction is a ``Decimal``: whole values are read, fractions never guessed."""
    lookup = await _call_with_number("lookup", "1080.0", recorded, harness, account, creds)
    assert lookup.total_amount_vnd == 1080
    lookup = await _call_with_number("lookup", "1080.5", recorded, harness, account, creds)
    assert lookup.total_amount_vnd is None
    facts = await _call_with_number("usage", "999.0", recorded, harness, account, creds)
    assert facts.quota_remaining == 999
    facts = await _call_with_number("usage", "1e3", recorded, harness, account, creds)
    assert facts.quota_remaining == 1000
    facts = await _call_with_number("usage", "999.5", recorded, harness, account, creds)
    assert facts.quota_remaining is None
    with pytest.raises(ProviderTransient):
        await _call_with_number("lookup", "NaN", recorded, harness, account, creds)


async def test_deeply_nested_json_body_is_transient_not_recursion_error(
    harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    hooks = 0

    async def hook() -> bool:
        nonlocal hooks
        hooks += 1
        return True

    h = harness(lambda request: _nested(200 if request.method == "POST" else 502))
    with pytest.raises(ProviderTransient):
        await h.provider.create(account, creds, {"reference_code": REFERENCE}, before_send=hook)
    with pytest.raises(ProviderTransient):
        await h.provider.check_create(account, creds, REFERENCE)
    with pytest.raises(ProviderTransient):
        await h.provider.find_by_reference(account, creds, REFERENCE)
    assert hooks == 1
    assert len(h.calls("/v1/invoices/create", "POST")) == 1


async def test_get_with_html_or_nested_body_is_transient(
    harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    for response in (_html(502), _nested(502), httpx.Response(503, content=b"\xff\xfe")):
        h = harness(lambda request, r=response: r)
        with pytest.raises(ProviderTransient):
            await h.provider.check_create(account, creds, REFERENCE)
    unreadable = harness(lambda request: _nested(200))
    assert (await unreadable.provider.check_create(account, creds, REFERENCE)).state == "pending"


@pytest.mark.parametrize("value", ["x/../../provider-accounts", "a b", "a?type=xml", "a#b", "%2F"])
async def test_path_segments_are_percent_encoded(
    value: str,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    success = copy.deepcopy(recorded("check_success.json")["body"])
    success["data"]["reference_code"] = value
    h = harness(lambda request: httpx.Response(200, json=success))
    await h.provider.check_create(account, creds, value)
    path = h.calls("/check/")[0].url.raw_path.decode()
    assert path == "/v1/invoices/create/check/" + quote(value, safe="")
    assert "/" not in path.removeprefix("/v1/invoices/create/check/")


async def test_provider_account_listing_reads_one_full_page(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView
) -> None:
    listing = copy.deepcopy(recorded("provider_accounts.json")["body"])
    listing["data"]["items"] += [
        {"active": "false", "id": "00000000-0000-0000-0000-0000000000f1"},
        {"active": "true", "id": "00000000-0000-0000-0000-0000000000f2"},
        {"active": 1, "id": "00000000-0000-0000-0000-0000000000f3"},
        {"active": None, "id": "00000000-0000-0000-0000-0000000000f4"},
        {"active": True, "id": ""},
    ]
    h = harness(lambda request: httpx.Response(200, json=listing))
    assert await h.provider.provider_account_ids(account) == (
        "00000000-0000-0000-0000-000000000000",
    )
    request = h.calls("/v1/provider-accounts")[0]
    assert request.url.path == "/v1/provider-accounts"
    assert dict(request.url.params) == {"per_page": "100"}
    listing["data"]["paging"]["has_more"] = "false"
    text = harness(lambda request: httpx.Response(200, json=listing))
    assert len(await text.provider.provider_account_ids(account)) == 1
    listing["data"]["paging"]["has_more"] = True
    more = harness(lambda request: httpx.Response(200, json=listing))
    with pytest.raises(ProviderConfigError):
        await more.provider.provider_account_ids(account)


@pytest.mark.parametrize(
    "items", [None, "x", [{"active": True, "id": {"x": 1}}, {"active": True, "id": 1.5}]]
)
async def test_provider_account_listing_takes_only_text_ids(
    items: object, harness: Callable[..., Any], account: IssuerAccountView
) -> None:
    listing = {"success": True, "data": {"items": items}}
    h = harness(lambda request: httpx.Response(200, json=listing))
    assert await h.provider.provider_account_ids(account) == ()


def _artifact(kind: str, content: bytes, file_type: str | None = None) -> dict[str, Any]:
    data = {"file_type": file_type or kind, "content": base64.b64encode(content).decode()}
    return {"success": True, "data": data}


@pytest.mark.parametrize(
    ("pdf", "xml", "code"),
    [
        (_artifact("pdf", b"notpdf"), _artifact("xml", b"<x/>"), "artifact_pdf_invalid"),
        (_artifact("pdf", b" %PDF-1.4"), _artifact("xml", b"<x/>"), "artifact_pdf_invalid"),
        (_artifact("pdf", b"%PDF-1.4", "xml"), _artifact("xml", b"<x/>"), "artifact_pdf_missing"),
        (_artifact("pdf", b"%PDF-1.4"), _artifact("xml", b"<x/>", "pdf"), "artifact_xml_missing"),
    ],
)
async def test_artifact_type_and_pdf_header_are_checked(
    pdf: dict[str, Any],
    xml: dict[str, Any],
    code: str,
    harness: Callable[..., Any],
    account: IssuerAccountView,
    creds: Any,
) -> None:
    bodies = {"pdf": pdf, "xml": xml}
    h = harness(lambda request: httpx.Response(200, json=bodies[request.url.params["type"]]))
    with pytest.raises(ProviderRejected) as raised:
        await h.provider.fetch_artifacts(account, creds, REFERENCE, "unused")
    assert raised.value.code == code


def test_provider_amounts_and_numbers_are_never_guessed(recorded: Loader) -> None:
    body = copy.deepcopy(recorded("get_by_reference.json")["body"])
    refused = (
        *("1080.50", "1080.5", Decimal("1080.5"), "NaN", "abc", True),
        *("Infinity", Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN")),
        *("1e3", "1e1000000", "1e999999999999999999", Decimal("1E+1000000")),
        *("+1080", " 1080 ", "1_080", "\u0661\u0660\u0668\u0660", "1080.", ".5"),
    )
    started = time.perf_counter()
    for total in refused:
        body["data"]["total_amount"] = total
        assert _lookup(body).total_amount_vnd is None
    assert time.perf_counter() - started < 2
    for total, expected in (
        ("1080.00", 1080),
        (Decimal("1080"), 1080),
        (1080, 1080),
        ("-1080", -1080),
        (Decimal("1E+3"), 1000),
    ):
        body["data"]["total_amount"] = total
        assert _lookup(body).total_amount_vnd == expected
    for number in (" 0", "0 ", "0", "", None):
        body["data"]["invoice_number"] = number
        assert _lookup(body).invoice_number is None


def test_parse_account_facts_skips_templates_without_both_codes(recorded: Loader) -> None:
    detail = copy.deepcopy(_detail_body(recorded))
    detail["data"]["templates"] += [
        {"invoice_series": "C26TXX"},
        {"template_code": "3"},
        {"template_code": None, "invoice_series": "C26TYY"},
        {"template_code": "", "invoice_series": "C26TZZ"},
        "4",
    ]
    facts = parse_account_facts("acc", detail, None)
    assert facts.templates == ("1", "2")
    assert facts.series == ("C26TSE", "C26TSE")


def test_parse_account_facts_reads_activation_and_approval_date(recorded: Loader) -> None:
    detail = _detail_body(recorded)
    assert detail["data"]["active"] is True
    facts = parse_account_facts("acc", detail, None)
    assert facts.active is True
    assert facts.tax_authority_approved_date is None
    documented = copy.deepcopy(detail)
    documented["data"]["tax_authority_approved_date"] = "2026-04-20"
    documented["data"]["active"] = False
    facts = parse_account_facts("acc", documented, None)
    assert facts.active is False
    assert facts.tax_authority_approved_date == date(2026, 4, 20)
    for active, approved in (("true", "20/04/2026"), (1, "2026-02-30"), (None, 20260420)):
        documented["data"]["active"] = active
        documented["data"]["tax_authority_approved_date"] = approved
        facts = parse_account_facts("acc", documented, None)
        assert facts.active is None
        assert facts.tax_authority_approved_date is None


async def test_provider_corrections_not_implemented(
    harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    h = harness(lambda request: httpx.Response(500))
    with pytest.raises(NotImplementedError, match=r"v0\.2"):
        await h.provider.adjust(account, creds, {})
    with pytest.raises(NotImplementedError, match=r"v0\.2"):
        await h.provider.replace(account, creds, {})
    with pytest.raises(NotImplementedError, match=r"v0\.2"):
        h.provider.build_adjust_payload()
    with pytest.raises(NotImplementedError, match=r"v0\.2"):
        h.provider.build_replace_payload()
    assert h.requests == []
