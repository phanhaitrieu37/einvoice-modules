import base64
import copy
import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
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
    ProviderRejected,
    ProviderTransient,
    SendAborted,
)

pytestmark = pytest.mark.contract

RECORDED = Path(__file__).parents[2] / "fixtures" / "sepay_einvoice"
Loader = Callable[[str], dict[str, Any]]
REFERENCE = "00000000-0000-4000-8000-00000000c0de"


def _detail_body(recorded: Loader) -> dict[str, Any]:
    return recorded("provider_accounts.json")["provider_account_details"][0]["body"]


# --- parsing recorded and synthetic responses -------------------------------------------


def test_parse_accepted_reads_tracking_code(recorded: Loader) -> None:
    record = recorded("create_accepted.json")
    accepted = parse_accepted(record["body"])
    assert accepted.tracking_code == record["body"]["data"]["tracking_code"]


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
    invoice = parse_invoice(body, REFERENCE)
    assert invoice.state == "success"
    assert invoice.total_amount_vnd == 1080
    assert invoice.invoice_number == "307614"
    assert invoice.issued_date == date(2026, 9, 25)
    assert invoice.reference_code == body["data"]["reference_code"]
    monkeypatch.setattr(payload, "LOOKUP_RETURNS_TOTAL", False)
    assert parse_invoice(body, REFERENCE).total_amount_vnd is None


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
    invoice = parse_invoice(recorded("draft_lookup.json")["body"], REFERENCE)
    assert invoice.state == "draft"
    assert invoice.invoice_number is None


def test_parse_progress_and_invoice_carry_provider_issued_date(recorded: Loader) -> None:
    check = recorded("check_success.json")["body"]
    lookup = recorded("issue_lookup_issued.json")["body"]
    assert parse_progress(check).issued_date == date(2026, 9, 25)
    assert parse_invoice(lookup, REFERENCE).issued_date == date(2026, 10, 7)
    check_without = copy.deepcopy(check)
    del check_without["data"]["invoice"]["issued_date"]
    lookup_without = copy.deepcopy(lookup)
    del lookup_without["data"]["issued_date"]
    assert parse_progress(check_without).issued_date is None
    assert parse_invoice(lookup_without, REFERENCE).issued_date is None
    assert parse_progress(check_without).state == "success"


def test_parse_draft_invoice_number_zero_is_no_number(recorded: Loader) -> None:
    check = recorded("draft_check.json")["body"]
    lookup = recorded("draft_lookup.json")["body"]
    assert check["data"]["invoice"]["invoice_number"] == "0"
    assert lookup["data"]["invoice_number"] == "0"
    assert parse_progress(check).invoice_number is None
    assert parse_progress(check).state == "draft"
    assert parse_invoice(lookup, REFERENCE).invoice_number is None
    assert parse_invoice(lookup, REFERENCE).state == "draft"


def test_parse_invoice_failed_message_present_or_absent(synthetic: Loader) -> None:
    with_message = synthetic("lookup_failed_with_message.json")
    without_message = synthetic("lookup_failed_without_message.json")
    assert with_message["_synthetic"] and without_message["_synthetic"]
    present = parse_invoice(with_message["body"], REFERENCE)
    assert present.state == "failed"
    assert present.provider_message == with_message["body"]["data"]["message"]
    assert present.invoice_number is None
    absent = parse_invoice(without_message["body"], REFERENCE)
    assert absent.state == "failed"
    assert absent.provider_message is None


def test_parse_invoice_unknown_status_is_never_success(recorded: Loader) -> None:
    body = copy.deepcopy(recorded("get_by_reference.json")["body"])
    body["data"]["status"] = "cancelled"
    body["data"]["message"] = "x"
    invoice = parse_invoice(body, REFERENCE)
    assert invoice.state == "pending"
    assert invoice.provider_message is None


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


async def test_provider_find_by_reference_missing_numbers_returns_incomplete(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    body = copy.deepcopy(recorded("get_by_reference.json")["body"])
    body["data"]["invoice_number"] = None
    del body["data"]["issued_date"]
    h = harness(lambda request: httpx.Response(200, json=body))
    invoice = await h.provider.find_by_reference(account, creds, REFERENCE)
    assert invoice is not None
    assert invoice.invoice_number is None
    assert invoice.issued_date is None
    assert invoice.total_amount_vnd == 1080
    assert [r.url.path for r in h.requests if r.url.path != "/v1/token"] == [
        f"/v1/invoices/{REFERENCE}"
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
    "307": (lambda r: _redirect(307), ProviderTransient, False),
    "308": (lambda r: _redirect(308), ProviderTransient, False),
    "timeout": (_raise(httpx.ReadTimeout("slow")), ProviderTransient, False),
    "5xx": (lambda r: httpx.Response(503, json={}), ProviderTransient, False),
    "connect_error": (_raise(httpx.ConnectError("down")), ProviderTransient, False),
}


def _unauthorized() -> httpx.Response:
    return httpx.Response(401, json={"success": False, "error": {"code": "UNAUTHORIZED"}})


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
