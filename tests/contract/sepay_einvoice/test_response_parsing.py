import base64
import copy
import json
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
REFERENCE = "00000000-0000-4000-8000-00000000c0de"


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


@pytest.mark.parametrize(
    ("status", "message", "error_code"),
    [
        ("Failed", UNCONFIRMED, "provider_unconfirmed"),
        ("Failed", f"  {UNCONFIRMED}\n", "provider_unconfirmed"),
        ("Pending", UNCONFIRMED, "provider_unconfirmed"),
        ("Failed", f"{UNCONFIRMED}.", None),
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
    """Docs: this exact message means the outcome is unknown and the request is not resent."""
    body = copy.deepcopy(recorded("check_success.json")["body"])
    body["data"]["status"] = status
    body["data"]["message"] = message
    progress = parse_progress(body)
    assert progress.error_code == error_code
    if status == "Failed":
        assert progress.provider_message == message


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


async def test_provider_find_by_reference_none_on_404(
    harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    not_found = {"success": False, "error": {"code": "NOT_FOUND", "message": "x"}}
    h = harness(lambda request: httpx.Response(404, json=not_found))
    assert await h.provider.find_by_reference(account, creds, REFERENCE) is None
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
    for content in ("not base64!", "SGVsbG8=?!", "", "\n"):
        bad = harness(lambda request, c=content: httpx.Response(200, json={"data": {"content": c}}))
        with pytest.raises(ProviderRejected):
            await bad.provider.fetch_artifacts(account, creds, REFERENCE, "unused")


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
}


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
        "issued_date_invalid",
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
    success = recorded("check_success.json")["body"]
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
    listing["data"]["paging"]["has_more"] = True
    more = harness(lambda request: httpx.Response(200, json=listing))
    with pytest.raises(ProviderConfigError):
        await more.provider.provider_account_ids(account)


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
    for total in ("1080.50", "1080.5", Decimal("1080.5"), "NaN", "abc", True):
        body["data"]["total_amount"] = total
        assert _lookup(body).total_amount_vnd is None
    for total, expected in (("1080.00", 1080), (Decimal("1080"), 1080), (1080, 1080)):
        body["data"]["total_amount"] = total
        assert _lookup(body).total_amount_vnd == expected
    for number in (" 0", "0 ", "0", "", None):
        body["data"]["invoice_number"] = number
        assert _lookup(body).invoice_number is None


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
