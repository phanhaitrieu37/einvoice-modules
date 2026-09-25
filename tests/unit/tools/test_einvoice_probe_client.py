import base64
import hashlib
import inspect
import json
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

from tools.einvoice_probe.candidates import CANDIDATES
from tools.einvoice_probe.client import SANDBOX_BASE_URL, ProbeClient
from tools.einvoice_probe.scenarios import (
    accounts,
    evidence,
    s1_create_poll,
    s2_duplicate_reference,
    s3_failed_then_retry,
    s4_download,
    s6_quota,
    write_json,
)

ENV = {
    "SEPAY_EINVOICE_ENVIRONMENT": "test",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "probe-client",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "probe-secret",
}
ISSUED = "2026-09-24 00:00:00"
LIST_ITEMS = [{"id": "account-1", "active": True}]
PAGING = {"per_page": 200, "total": 1, "has_more": False, "current_page": 1, "page_count": 1}
LIST_BODY = {"success": True, "data": {"items": LIST_ITEMS, "paging": PAGING}}
DETAIL_BODY = {
    "success": True,
    "data": {
        "id": "account-1",
        "templates": [{"template_code": "1", "invoice_series": "C26TSE"}],
    },
}


class FakeClock:
    def __init__(self) -> None:
        self.current = 0.0

    def __call__(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


def client_with(handler):
    return ProbeClient(ENV, httpx.MockTransport(handler))


def token(request):
    assert request.url.path == "/v1/token"
    auth = "Basic " + base64.b64encode(b"probe-client:probe-secret").decode()
    assert request.headers["Authorization"] == auth
    return httpx.Response(
        200,
        json={
            "success": True,
            "data": {"access_token": "mock.jwt.token", "token_type": "Bearer", "expires_in": 86400},
        },
    )


def setup_accounts(request):
    if request.url.path == "/v1/token":
        return token(request)
    if request.url.path == "/v1/provider-accounts":
        return httpx.Response(200, json=LIST_BODY)
    if request.url.path == "/v1/provider-accounts/account-1":
        return httpx.Response(200, json=DETAIL_BODY)
    return None


def paging(items):
    return {
        "items": items,
        "paging": {
            "per_page": 200,
            "total": len(items),
            "has_more": False,
            "current_page": 1,
            "page_count": 1,
        },
    }


def download_response(request: httpx.Request) -> httpx.Response:
    kind = request.url.params["type"]
    payload = b"PDF-SANDBOX" if kind == "pdf" else b"<invoice>sandbox</invoice>"
    name = "HD_0000589_20251215.pdf" if kind == "pdf" else "HD_0000589_20251215.xml"
    return httpx.Response(
        200,
        json={
            "success": True,
            "data": {
                "file_type": kind,
                "file_name": name,
                "content": base64.b64encode(payload).decode(),
            },
        },
    )


def check_response(status: str, **values) -> httpx.Response:
    data = {
        "status": status,
        "retry": {"next_retry_at": None},
        "invoice": {"xml_url": None},
        **values,
    }
    return httpx.Response(200, json={"success": True, "data": data})


def test_client_refuses_non_test_environment() -> None:
    try:
        ProbeClient(
            {**ENV, "SEPAY_EINVOICE_ENVIRONMENT": "live"},
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )
    except ValueError as error:
        assert "must be test" in str(error)
    else:
        raise AssertionError("non-test environment was accepted")


def test_client_has_no_base_url_override() -> None:
    assert list(inspect.signature(ProbeClient).parameters) == ["env", "transport"]
    assert SANDBOX_BASE_URL == "https://einvoice-api-sandbox.sepay.vn"
    assert all(not entry["verified"] for entry in CANDIDATES.values())
    assert CANDIDATES["provider_account_detail"]["path"] == "/v1/provider-accounts/{id}"
    assert CANDIDATES["quota"]["path"] == "/v1/usage"
    assert CANDIDATES["publish"]["path"] == "/v1/invoices/issue"
    assert CANDIDATES["get_by_reference"]["source"] == "chi-tiet-hoa-don"
    with client_with(lambda _: httpx.Response(200)) as client:
        try:
            client.request_json("GET", "https://example.com/", authenticated=False)
        except ValueError as error:
            assert "sandbox" in str(error)
        else:
            raise AssertionError("non-sandbox host was accepted")

        trailing_host = SANDBOX_BASE_URL.removeprefix("https://") + "."
        try:
            client.request_json("GET", f"https://{trailing_host}/v1/token", authenticated=False)
        except ValueError:
            pass
        else:
            raise AssertionError("trailing-dot SePay host was accepted")


def test_api_candidate_routes_match_raw_docs() -> None:
    assert CANDIDATES["get_by_reference"]["path"] == "/v1/invoices/{reference_code}"
    assert CANDIDATES["get_by_reference"]["source"] == "chi-tiet-hoa-don"
    assert CANDIDATES["list_invoices"]["path"] == "/v1/invoices"
    assert CANDIDATES["list_invoices"]["source"] == "danh-sach-hoa-don"
    assert CANDIDATES["download"]["parameter"] == "type=pdf|xml"
    assert CANDIDATES["download"]["source"] == "tai-hoa-don"
    assert CANDIDATES["download"]["identifier"] == "tracking_code"
    assert CANDIDATES["download"]["fallback_identifier"] == "reference_code"
    assert CANDIDATES["download"]["fallback_source"] == "chi-tiet-hoa-don"
    assert CANDIDATES["quota"]["path"] == "/v1/usage"
    assert CANDIDATES["quota"]["source"] == "kiem-tra-han-ngach"
    assert CANDIDATES["publish"]["path"] == "/v1/invoices/issue"
    assert CANDIDATES["publish"]["source"] == "phat-hanh-hoa-don-dien-tu"


def test_account_template_and_series_selectors_are_explicit() -> None:
    from tools.einvoice_probe.scenarios import select_provider_account

    response = {
        "body": {
            "data": [
                {
                    "id": "account-1",
                    "active": True,
                    "templates": [
                        {"template_code": "1", "invoice_series": "C26TSE"},
                        {"template_code": "1", "invoice_series": "C27TSE"},
                        {"template_code": "2", "invoice_series": "C26TSE"},
                    ],
                }
            ]
        }
    }
    selected = select_provider_account(response, template_code="1", invoice_series="C27TSE")
    assert selected["template_code"] == "1"
    assert selected["invoice_series"] == "C27TSE"


def test_ambiguous_template_error_suggests_selector() -> None:
    from tools.einvoice_probe.scenarios import select_provider_account

    account = {
        "id": "account-1",
        "active": True,
        "templates": [
            {"template_code": "1", "invoice_series": "C26TSE"},
            {"template_code": "2", "invoice_series": "C26TSE"},
        ],
    }
    try:
        select_provider_account({"body": {"data": {"items": [account], "paging": PAGING}}})
    except RuntimeError as error:
        assert "--template-code" in str(error)
        assert "Multiple invoice templates or series are available" in str(error)
        assert "found 0" not in str(error)
    else:
        raise AssertionError("ambiguous account should request a selector")


def test_account_selection_rejects_zero_and_ambiguous_matches() -> None:
    from tools.einvoice_probe.scenarios import select_provider_account

    valid_record = {
        "id": "account-1",
        "active": True,
        "template_code": "1",
        "invoice_series": "C26TSE",
    }
    for rows, count in [([], 0), ([valid_record, valid_record], 2)]:
        try:
            select_provider_account({"body": {"data": rows}})
        except RuntimeError as error:
            assert f"found {count}" in str(error)
            if count:
                assert "--template-code" in str(error)
        else:
            raise AssertionError("ambiguous provider account selection was accepted")


def test_account_details_are_loaded_from_documented_endpoint() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/token":
            return token(request)
        if request.url.path == "/v1/provider-accounts/account-1":
            return httpx.Response(200, json=DETAIL_BODY)
        raise AssertionError(request.url.path)

    from tools.einvoice_probe.scenarios import select_provider_account

    with client_with(handler) as probe:
        selected = select_provider_account(
            {"body": {"data": [{"id": "account-1", "active": True}]}},
            probe,
            template_code="1",
        )
    assert selected["template_code"] == "1"
    assert selected["invoice_series"] == "C26TSE"
    assert requests == ["/v1/token", "/v1/provider-accounts/account-1"]


def test_redact_camel_case_secret_keys() -> None:
    from tools.einvoice_probe.redact import redact

    assert redact({"accessToken": "secret", "clientSecret": "secret"}) == {
        "accessToken": "REDACTED",
        "clientSecret": "REDACTED",
    }


def test_token_never_written_to_output(tmp_path: Path, capsys, caplog) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = setup_accounts(request)
        return result or httpx.Response(200, json=LIST_BODY)

    with client_with(handler) as probe:
        probe.request_json("GET", "/v1/provider-accounts")
        write_json(tmp_path, "token", probe.token_response)
    saved = (tmp_path / "token.json").read_text(encoding="utf-8")
    combined = capsys.readouterr().out + caplog.text
    assert requests[1].headers["Authorization"] == "Bearer mock.jwt.token"
    assert "mock.jwt.token" not in combined
    assert "mock.jwt.token" not in saved
    assert '"access_token": "REDACTED"' in saved
    assert "probe-secret" not in saved + combined


def test_accounts_writes_redacted_token_and_account_responses(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        setup = setup_accounts(request)
        if setup:
            return setup
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        accounts(probe, tmp_path)
    assert (tmp_path / "token_response.json").is_file()
    assert (tmp_path / "provider_accounts.json").is_file()
    assert (tmp_path / "provider_account_detail_001.json").is_file()
    token_body = json.loads((tmp_path / "token_response.json").read_text())
    assert token_body["body"]["data"]["access_token"] == "REDACTED"
    assert "mock.jwt.token" not in (tmp_path / "token_response.json").read_text()


def test_s1_flow_with_mock_transport(monkeypatch) -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.sleep(seconds)

    polls = 0
    creates: list[dict] = []
    create_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            create_methods.append(request.method)
            creates.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "track-1"}})
        if request.url.path.endswith("/track-1"):
            polls += 1
            state = "Pending" if polls == 1 else "Success"
            data = {"status": state}
            if state == "Pending":
                data["retry"] = {"next_retry_at": "2026-09-24 12:00:00"}
            if state == "Success":
                data["invoice"] = {"pdf_url": "/sandbox/p.pdf", "xml_url": None}
                data["retry"] = {"next_retry_at": None}
            return httpx.Response(200, json={"success": True, "data": data})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s1_create_poll(probe, tax_rate="10", issued_date=ISSUED, sleep=sleep, clock=clock)
    assert result["scenario"] == "PASS"
    assert [row["body"]["data"]["status"] for row in result["polls"]] == ["Pending", "Success"]
    assert result["polls"][0]["retry_next_at"] == "2026-09-24 12:00:00"
    payload = creates[0]
    assert payload["provider_account_id"] == "account-1"
    assert payload["template_code"] == "1"
    assert payload["invoice_series"] == "C26TSE"
    assert payload["issued_date"] == ISSUED
    assert payload["currency"] == "VND"
    assert payload["is_draft"] is False
    assert payload["items"][0]["tax_rate"] == 10
    assert payload["items"][0]["item_name"] == "Probe item"
    assert payload["total_amount"] == 1100
    assert sleeps == [2.5]
    assert create_methods == ["POST"]


def test_s2_records_409_and_lookup(monkeypatch) -> None:
    clock = FakeClock()
    creates = 0
    paths: list[str] = []
    sent_reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, sent_reference
        paths.append(request.url.path)
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            body = json.loads(request.content)
            if creates == 1:
                sent_reference = body["reference_code"]
            status = 200 if creates == 1 else 409
            body = (
                {"success": True, "data": {"tracking_code": "tr-1"}}
                if status == 200
                else {
                    "success": False,
                    "error": {"code": "DOCUMENT_EXISTS", "message": "Already exists"},
                }
            )
            return httpx.Response(status, json=body)
        if request.url.path.endswith("/tr-1"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        if request.url.path == f"/v1/invoices/{sent_reference}":
            return httpx.Response(404, json={"success": False})
        if request.url.path == "/v1/invoices":
            return httpx.Response(
                200,
                json={"success": True, "data": paging([{"reference_code": sent_reference}])},
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "PASS"
    assert result["first"]["status"] == 200
    assert result["first_polls"][-1]["body"]["data"]["status"] == "Success"
    assert result["first_polls"][-1]["retry_next_at"] is None
    assert result["duplicate"]["status"] == 409
    assert [item["status"] for item in result["lookups"]] == [404, 200]
    assert [item["matched"] for item in result["lookups"]] == [False, True]
    assert "/v1/invoices" in paths


def test_s2_pending_first_create_is_not_a_pass() -> None:
    clock = FakeClock()
    creates = 0
    sent_reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, sent_reference
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                sent_reference = json.loads(request.content)["reference_code"]
            status = 200 if creates == 1 else 409
            return httpx.Response(
                status,
                json=(
                    {"success": True, "data": {"tracking_code": "pending"}}
                    if status == 200
                    else {
                        "success": False,
                        "error": {"code": "DOCUMENT_EXISTS", "message": "Already exists"},
                    }
                ),
            )
        if request.url.path.endswith("/pending"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Pending",
                        "retry": {"next_retry_at": "2026-09-24 12:00:00"},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(
                404,
                json={
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Invoice not found"},
                },
            )
        if request.url.path == "/v1/invoices":
            return httpx.Response(
                200,
                json={"success": True, "data": paging([{"reference_code": sent_reference}])},
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["first_poll_exhausted"] is True
    assert result["first_state"] == "Pending"
    assert result["scenario"] == "FAIL"


def test_s2_lookup_must_match_reference_code() -> None:
    creates = 0
    sent_reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, sent_reference
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "track"}}
                )
            return httpx.Response(
                409,
                json={
                    "success": False,
                    "error": {"code": "DOCUMENT_EXISTS", "message": "Already exists"},
                },
            )
        if request.url.path.endswith("/track"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(
                404, json={"success": False, "error": {"code": "NOT_FOUND", "message": "Not found"}}
            )
        if request.url.path == "/v1/invoices":
            unrelated = {"reference_code": "different-reference"}
            return httpx.Response(200, json={"success": True, "data": paging([unrelated])})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["lookups"][0]["status"] == 404
    assert result["lookups"][1]["status"] == 200
    assert all(not row["matched"] for row in result["lookups"])
    assert result["scenario"] == "FAIL"


def test_s2_duplicate_without_409_is_fail() -> None:
    creates = 0
    sent_reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, sent_reference
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            sent_reference = json.loads(request.content)["reference_code"]
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "track"}})
        if request.url.path.endswith("/track"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        if request.url.path == f"/v1/invoices/{sent_reference}":
            return httpx.Response(
                200, json={"success": True, "data": {"reference_code": sent_reference}}
            )
        if request.url.path == "/v1/invoices":
            return httpx.Response(
                200, json={"success": True, "data": paging([{"reference_code": sent_reference}])}
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["first_state"] == "Success"
    assert result["duplicate"]["status"] == 200
    assert result["scenario"] == "FAIL"


def test_s2_rejected_first_create_is_fail() -> None:
    creates = 0
    sent_reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, sent_reference
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                sent_reference = json.loads(request.content)["reference_code"]
                return httpx.Response(
                    400,
                    json={
                        "success": False,
                        "error": {"code": "BAD_REQUEST", "message": "Invalid request"},
                    },
                )
            return httpx.Response(
                409,
                json={
                    "success": False,
                    "error": {"code": "DOCUMENT_EXISTS", "message": "Already exists"},
                },
            )
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(
                404, json={"success": False, "error": {"code": "NOT_FOUND", "message": "Not found"}}
            )
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": paging([])})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(probe, tax_rate=10, issued_date=ISSUED)
    assert result["first"]["status"] == 400
    assert result["first_state"] is None
    assert result["first_polls"] == []
    assert result["scenario"] == "FAIL"


def test_s2_failed_first_create_is_not_successful() -> None:
    creates = 0
    reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, reference
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                reference = json.loads(request.content)["reference_code"]
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "failed-track"}}
                )
            return httpx.Response(
                409,
                json={
                    "success": False,
                    "error": {"code": "DOCUMENT_EXISTS", "message": "Already exists"},
                },
            )
        if request.url.path.endswith("/failed-track"):
            return check_response("Failed")
        if request.url.path == f"/v1/invoices/{reference}":
            return httpx.Response(
                200, json={"success": True, "data": {"reference_code": reference, "xml_url": None}}
            )
        if request.url.path == "/v1/invoices":
            return httpx.Response(
                200, json={"success": True, "data": paging([{"reference_code": reference}])}
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["first_state"] == "Failed"
    assert result["scenario"] == "FAIL"


def test_s2_detail_with_different_reference_does_not_match() -> None:
    creates = 0
    reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, reference
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                reference = json.loads(request.content)["reference_code"]
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "track"}}
                )
            return httpx.Response(
                409,
                json={
                    "success": False,
                    "error": {"code": "DOCUMENT_EXISTS", "message": "Already exists"},
                },
            )
        if request.url.path.endswith("/track"):
            return check_response("Success")
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"reference_code": "different-reference", "xml_url": None},
                },
            )
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": paging([])})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["lookups"][0]["status"] == 200
    assert result["lookups"][0]["matched"] is False
    assert result["scenario"] == "FAIL"


def test_s3_sync_reject_is_inconclusive(monkeypatch) -> None:
    clock = FakeClock()
    creates = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "control"}}
                )
            return httpx.Response(422, json={"success": False, "error": {"code": "INVALID"}})
        if request.url.path.endswith("/control"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["variant"] == "sync_reject"
    assert result["error_code"] == "INVALID"
    assert result["scenario"] == "INCONCLUSIVE"


def test_evidence_file_shape(monkeypatch) -> None:
    clock = FakeClock()
    created_by_reference: dict[str, int] = {}
    track_count = 0
    last_reference = ""
    request_urls: list[str] = []
    download_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal track_count, last_reference
        request_urls.append(str(request.url))
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            payload = json.loads(request.content)
            reference = payload["reference_code"]
            last_reference = reference
            created_by_reference[reference] = created_by_reference.get(reference, 0) + 1
            if created_by_reference[reference] > 1:
                return httpx.Response(
                    409,
                    json={
                        "success": False,
                        "error": {"code": "DOCUMENT_EXISTS", "message": "Invoice already exists"},
                    },
                )
            track_count += 1
            return httpx.Response(
                200, json={"success": True, "data": {"tracking_code": f"track-{track_count}"}}
            )
        if "/create/check/" in request.url.path:
            tracking = request.url.path.rsplit("/", 1)[-1]
            state = "Failed" if tracking == "track-3" else "Success"
            invoice = {
                "pdf_url": "https://files.example.test/p.pdf?signature=private",
                "xml_url": None,
            }
            data = {"status": state, "retry": {"next_retry_at": None}, "invoice": invoice}
            return httpx.Response(200, json={"success": True, "data": data})
        if request.url.path.endswith("/download"):
            download_types.append(request.url.params["type"])
            return download_response(request)
        if request.url.path == "/v1/usage":
            return httpx.Response(200, json={"success": True, "data": {"quota_remaining": "534"}})
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(
                404,
                json={
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Invoice not found"},
                },
            )
        if request.url.path == "/v1/invoices":
            return httpx.Response(
                200, json={"success": True, "data": paging([{"reference_code": last_reference}])}
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = evidence(probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock)
    assert result["environment"] == "test"
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None
    assert result["details"]["s3_variant"] == "async_failed"
    assert result["details"]["s3_variant"] == result["details"]["s3"]["variant"]
    assert result["scenarios"] == {
        "s1": "PASS",
        "s2": "PASS",
        "s3": "INCONCLUSIVE",
        "s4": "PASS",
        "s6": "PASS",
    }
    assert result["details"]["s3"]["retry_create_status"] == 409
    assert result["details"]["s3"]["retry"]["polls"] == []
    assert set(result["details"]["s4"]["downloads"]) == {"pdf", "xml"}
    expected_files = {"pdf": b"PDF-SANDBOX", "xml": b"<invoice>sandbox</invoice>"}
    for kind, item in result["details"]["s4"]["downloads"].items():
        assert item["sha256"] == hashlib.sha256(expected_files[kind]).hexdigest()
        assert item["size"] == len(expected_files[kind])
        assert item["file_type"] == kind
        assert item["file_name"].endswith(f".{kind}")
    assert result["details"]["s4"]["xml_url"] is None
    assert download_types == ["pdf", "xml"]
    assert all("files.example.test" not in url for url in request_urls)
    s4_tracking = result["details"]["s4"]["issue"]["tracking_code"]
    download_paths = [httpx.URL(url).path for url in request_urls if "/download" in url]
    assert download_paths == [f"/v1/invoices/{s4_tracking}/download"] * 2
    for item in result["details"]["s4"]["downloads"].values():
        assert item["identifier"] == "tracking_code"
        assert item["attempts"] == [{"identifier": "tracking_code", "status": 200}]


def test_evidence_async_failed_resend_success_is_pass(monkeypatch) -> None:
    clock = FakeClock()
    created_by_reference: dict[str, int] = {}
    reference_by_track: dict[str, str] = {}
    failed_references: set[str] = set()
    track_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal track_count
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            payload = json.loads(request.content)
            ref = payload["reference_code"]
            created_by_reference[ref] = created_by_reference.get(ref, 0) + 1
            if created_by_reference[ref] > 1 and ref not in failed_references:
                return httpx.Response(409, json={"success": False})
            track_count += 1
            tracking = f"evidence-track-{track_count}"
            reference_by_track[tracking] = ref
            return httpx.Response(
                200,
                json={"success": True, "data": {"tracking_code": tracking}},
            )
        if "/create/check/" in request.url.path:
            tracking = request.url.path.rsplit("/", 1)[-1]
            state = "Failed" if tracking == "evidence-track-3" else "Success"
            if state == "Failed":
                failed_references.add(reference_by_track[tracking])
            invoice = {"pdf_url": "/sandbox/a.pdf", "xml_url": None}
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"status": state, "retry": {"next_retry_at": None}, "invoice": invoice},
                },
            )
        if request.url.path.endswith("/download"):
            return download_response(request)
        if request.url.path == "/v1/usage":
            return httpx.Response(200, json={"success": True, "data": {"quota_remaining": "534"}})
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(200, json={"success": True, "data": {}})
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": paging([])})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = evidence(probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock)
    assert result["scenarios"]["s3"] == "PASS"
    assert result["details"]["s3"]["retry_create_status"] == 200


def test_evidence_download_metadata_must_match_requested_type(monkeypatch) -> None:
    clock = FakeClock()
    creates = 0
    downloads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, downloads
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            return httpx.Response(
                200, json={"success": True, "data": {"tracking_code": f"t{creates}"}}
            )
        if "/create/check/" in request.url.path:
            return check_response("Success", invoice={"pdf_url": "/sandbox/p.pdf", "xml_url": None})
        if request.url.path.endswith("/download"):
            downloads += 1
            response = download_response(request)
            body = response.json()
            if request.url.params["type"] == "xml":
                body["data"]["file_type"] = "pdf"
            return httpx.Response(200, json=body)
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s4_download(probe, tax_rate=10, issued_date=ISSUED, sleep=clock.sleep, clock=clock)
    assert result["downloads"]["xml"]["file_type"] == "pdf"
    assert result["scenario"] == "FAIL"
    assert downloads == 2


def test_probe_tax_rounding_edge_matrix() -> None:
    from decimal import Decimal

    from tools.einvoice_probe.scenarios import _as_decimal, _line_tax_amount

    unit = Decimal("1000")
    assert _line_tax_amount(unit, Decimal("-2")) == 0
    assert _line_tax_amount(unit, Decimal("-1")) == 0
    assert _line_tax_amount(unit, Decimal("0")) == 0
    assert _line_tax_amount(unit, Decimal("10")) == 100
    assert _line_tax_amount(unit, Decimal("10"), tax_amount=None) == 100
    assert _line_tax_amount(unit, Decimal("10"), tax_amount=0) == 0
    for value, error_type in [
        ("-3", ValueError),
        ("NaN", ValueError),
        ("Infinity", ValueError),
        ("10.5", ValueError),
        ("7", ValueError),
        (None, ValueError),
        (10.0, TypeError),
    ]:
        try:
            _line_tax_amount(unit, _as_decimal(value, "tax_rate"))
        except error_type:
            pass
        else:
            raise AssertionError(f"expected {error_type.__name__} for {value!r}")
    for value, error_type in [
        (-1, ValueError),
        ("NaN", ValueError),
        ("Infinity", ValueError),
        (0.0, TypeError),
    ]:
        try:
            _line_tax_amount(unit, Decimal("10"), tax_amount=value)
        except error_type:
            pass
        else:
            raise AssertionError(f"expected {error_type.__name__} for tax {value!r}")
    for amount in ("NaN", "Infinity"):
        try:
            _line_tax_amount(_as_decimal(amount, "unit_price"), Decimal("10"))
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected finite validation for {amount}")


def test_s4_download_404_marks_evidence_failed(monkeypatch) -> None:
    clock = FakeClock()
    created = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            created += 1
            return httpx.Response(
                200, json={"success": True, "data": {"tracking_code": f"track-{created}"}}
            )
        if "/create/check/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {
                            "pdf_url": "https://docs.example.test/p.pdf?private=x",
                            "xml_url": None,
                        },
                    },
                },
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(
                404,
                json={
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Tracking code not found"},
                },
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "FAIL"
    assert result["downloads"]["pdf"]["status"] == 404
    assert result["downloads"]["xml"]["status"] == 404


def test_s4_issue_failure_stays_fail_even_when_downloads_succeed() -> None:
    downloads = 0
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal downloads
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "track"}})
        if request.url.path.endswith("/create/check/track"):
            return check_response("Failed")
        if request.url.path.endswith("/download"):
            downloads += 1
            return download_response(request)
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s4_download(probe, tax_rate=10, issued_date=ISSUED, sleep=clock.sleep, clock=clock)
    assert result["issue"]["scenario"] == "FAIL"
    assert result["scenario"] == "FAIL"
    assert downloads == 2


def test_s1_create_400_is_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(
                400,
                json={
                    "success": False,
                    "error": {"code": "BAD_REQUEST", "message": "Invalid request"},
                },
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s1_create_poll(probe, tax_rate=10, issued_date=ISSUED)
    assert result["scenario"] == "FAIL"
    assert result["polls"] == []


def test_s1_check_4xx_stops_polling_as_inconclusive() -> None:
    checks = 0
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal checks
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "track"}})
        if request.url.path.endswith("/track"):
            checks += 1
            return httpx.Response(
                404,
                json={
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "Tracking code not found"},
                },
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s1_create_poll(
            probe, tax_rate=10, issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "INCONCLUSIVE"
    assert len(result["polls"]) == 1
    assert checks == 1


def test_artifact_download_uses_sandbox_type_and_base64_content() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path == "/v1/token":
            return token(request)
        return download_response(request)

    from tools.einvoice_probe.scenarios import _download_artifact

    with client_with(handler) as probe:
        artifact = _download_artifact(probe, "tracking-code", "pdf")
    expected = b"PDF-SANDBOX"
    assert artifact == {
        "status": 200,
        "size": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
        "file_type": "pdf",
        "file_name": "HD_0000589_20251215.pdf",
        "content_type": "application/json",
    }
    download = next(item for item in observed if item.url.path.endswith("/download"))
    assert download.url.host == "einvoice-api-sandbox.sepay.vn"
    assert download.url.params["type"] == "pdf"
    assert download.headers["Authorization"] == "Bearer mock.jwt.token"


def test_s3_async_failed_retries_same_reference_and_polls_success(monkeypatch) -> None:
    clock = FakeClock()
    create_count = 0
    create_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            create_count += 1
            payload = json.loads(request.content)
            create_payloads.append(payload)
            if create_count == 1:
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "control"}}
                )
            if create_count == 2:
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "failed"}}
                )
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "retry"}})
        if request.url.path.endswith("/control"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        if request.url.path.endswith("/failed"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Failed",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        if request.url.path.endswith("/retry"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "PASS"
    assert result["retry_create_status"] == 200
    assert create_payloads[1]["reference_code"] == create_payloads[2]["reference_code"]
    assert "tax_code" in create_payloads[1]["buyer"]
    assert "tax_code" not in create_payloads[2]["buyer"]


def test_s3_resend_ending_failed_is_inconclusive() -> None:
    clock = FakeClock()
    creates = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            tracking = {1: "control", 2: "original", 3: "retry"}[creates]
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": tracking}})
        state = "Success" if request.url.path.endswith("/control") else "Failed"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "status": state,
                    "retry": {"next_retry_at": None},
                    "invoice": {"xml_url": None},
                },
            },
        )

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate=10, issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["variant"] == "async_failed"
    assert result["retry"]["polls"][-1]["body"]["data"]["status"] == "Failed"
    assert result["scenario"] == "INCONCLUSIVE"


def test_nonexistent_tax_code_has_valid_checksum() -> None:
    from tools.einvoice_probe.scenarios import INVALID_NONEXISTENT_TAX_CODE

    digits = [int(character) for character in INVALID_NONEXISTENT_TAX_CODE]
    weights = [31, 29, 23, 19, 17, 13, 7, 5, 3]
    checksum = (
        10 - sum(digit * weight for digit, weight in zip(digits[:9], weights, strict=True)) % 11
    ) % 10
    assert digits[-1] == checksum


def test_s3_sync_reject_requires_successful_control_create() -> None:
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            create_count += 1
            status = 503 if create_count == 1 else 422
            return httpx.Response(status, json={"success": False, "error": {"code": "REJECT"}})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s3_failed_then_retry(probe, tax_rate="10", issued_date=ISSUED)
    assert result["variant"] == "not_triggerable"
    assert result["reason"] == "control_create_failed"
    assert result["error_code"] == "REJECT"


def test_s3_404_is_not_triggerable(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.scenarios.time.sleep", lambda _: None)
    creates = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            status = 200 if creates == 1 else 404
            body = (
                {"success": True, "data": {"tracking_code": "control"}}
                if status == 200
                else {"success": False, "error": {"code": "NOT_FOUND", "message": "Not found"}}
            )
            return httpx.Response(status, json=body)
        if request.url.path.endswith("/control"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "retry": {"next_retry_at": None},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s3_failed_then_retry(probe, tax_rate="10", issued_date=ISSUED)
    assert result["variant"] == "not_triggerable"
    assert result["create_status"] == 404
    assert result["reason"] == "create_http_404"
    assert result["create_status"] == 404


def test_s3_pending_at_bound_is_reported(monkeypatch) -> None:
    clock = FakeClock()
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "control"}})
        if request.url.path.endswith("/control"):
            polls += 1
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Pending",
                        "retry": {"next_retry_at": "2026-09-24 13:00:00"},
                        "invoice": {"xml_url": None},
                    },
                },
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s1_create_poll(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "INCONCLUSIVE"
    assert result["poll_exhausted"] is True
    assert len(result["polls"]) == 61
    assert clock.current == 150


def test_s3_pending_at_bound_records_reason(monkeypatch) -> None:
    clock = FakeClock()
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            create_count += 1
            return httpx.Response(
                200,
                json={"success": True, "data": {"tracking_code": f"track-{create_count}"}},
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "status": "Pending",
                    "retry": {"next_retry_at": "2026-09-24 13:00:00"},
                    "invoice": {"xml_url": None},
                },
            },
        )

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["poll_exhausted"] is True
    assert result["reason"] == "pending_at_bound"
    assert result["variant"] == "not_triggerable"
    assert result["scenario"] == "INCONCLUSIVE"


def test_s3_pending_then_failed_records_each_poll() -> None:
    create_count = 0
    poll_count = 0
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count, poll_count
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            create_count += 1
            code = "control" if create_count == 1 else "failed-track"
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": code}})
        if request.url.path.endswith("failed-track"):
            poll_count += 1
            state = "Pending" if poll_count == 1 else "Failed"
            return check_response(state)
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["variant"] == "async_failed"
    assert [item["body"]["data"]["status"] for item in result["polls"]] == [
        "Pending",
        "Failed",
    ]


def test_evidence_s1_failed_and_pending_statuses() -> None:
    from tools.einvoice_probe.scenarios import _success

    assert (
        _success(
            {
                "create": {"status": 200},
                "polls": [{"status": 200, "body": {"data": {"status": "Failed"}}}],
            }
        )
        == "FAIL"
    )
    assert (
        _success(
            {
                "create": {"status": 200},
                "polls": [{"status": 200, "body": {"data": {"status": "Pending"}}}],
                "poll_exhausted": True,
            }
        )
        == "INCONCLUSIVE"
    )


def test_evidence_preserves_s1_failed_and_pending_status(monkeypatch) -> None:
    import tools.einvoice_probe.scenarios as scenarios

    monkeypatch.setattr(
        scenarios,
        "select_provider_account",
        lambda *args, **kwargs: {
            "provider_account_id": "a",
            "template_code": "1",
            "invoice_series": "s",
        },
    )
    monkeypatch.setattr(scenarios, "accounts", lambda *_: {"body": {"data": []}})
    monkeypatch.setattr(
        scenarios,
        "s1_create_poll",
        lambda *args, **kwargs: {
            "scenario": "FAIL",
            "create": {"status": 200},
            "polls": [{"body": {"data": {"status": "Failed"}}}],
        },
    )
    monkeypatch.setattr(
        scenarios, "s2_duplicate_reference", lambda *args, **kwargs: {"scenario": "INCONCLUSIVE"}
    )
    monkeypatch.setattr(
        scenarios,
        "s3_failed_then_retry",
        lambda *args, **kwargs: {"scenario": "INCONCLUSIVE", "variant": "not_triggerable"},
    )
    monkeypatch.setattr(scenarios, "s4_download", lambda *args, **kwargs: {"scenario": "FAIL"})
    monkeypatch.setattr(scenarios, "s6_quota", lambda *_: {"scenario": "ABSENT"})
    result = scenarios.evidence(object(), tax_rate="10", issued_date=ISSUED)
    assert result["scenarios"]["s1"] == "FAIL"
    monkeypatch.setattr(
        scenarios,
        "s1_create_poll",
        lambda *args, **kwargs: {
            "scenario": "INCONCLUSIVE",
            "create": {"status": 200},
            "polls": [{"body": {"data": {"status": "Pending"}}}],
            "poll_exhausted": True,
        },
    )
    result = scenarios.evidence(object(), tax_rate="10", issued_date=ISSUED)
    assert result["scenarios"]["s1"] == "INCONCLUSIVE"


def test_evidence_persists_each_scenario_after_errors(tmp_path: Path, monkeypatch) -> None:
    import tools.einvoice_probe.scenarios as scenarios

    monkeypatch.setattr(
        scenarios,
        "accounts",
        lambda *_: {
            "body": {"success": True, "data": paging([])},
            "provider_account_details": [],
        },
    )
    monkeypatch.setattr(
        scenarios,
        "select_provider_account",
        lambda *args, **kwargs: {
            "provider_account_id": "account-1",
            "template_code": "1",
            "invoice_series": "C26TSE",
        },
    )

    def fail_s1(*_args, **_kwargs):
        raise RuntimeError("offline failure")

    monkeypatch.setattr(scenarios, "s1_create_poll", fail_s1)
    monkeypatch.setattr(
        scenarios, "s2_duplicate_reference", lambda *_args, **_kwargs: {"scenario": "INCONCLUSIVE"}
    )
    monkeypatch.setattr(
        scenarios,
        "s3_failed_then_retry",
        lambda *_args, **_kwargs: {"scenario": "INCONCLUSIVE", "variant": "not_triggerable"},
    )
    monkeypatch.setattr(scenarios, "s4_download", lambda *_args, **_kwargs: {"scenario": "FAIL"})
    monkeypatch.setattr(scenarios, "s6_quota", lambda *_args: {"scenario": "ABSENT"})
    saved: list[str] = []

    def save(name: str, result: dict) -> None:
        saved.append(name)
        write_json(tmp_path, f"evidence_{name}", result)

    result = scenarios.evidence(object(), tax_rate=10, issued_date=ISSUED, on_result=save)
    assert saved == ["accounts", "s1", "s2", "s3", "s4", "s6"]
    assert result["details"]["s1"]["error_class"] == "RuntimeError"
    assert json.loads((tmp_path / "evidence_s1.json").read_text())["error_class"] == "RuntimeError"


def test_write_json_redacts_credentials_jwt_buyer_and_account(tmp_path: Path) -> None:
    raw_token = "eyJabcdefghijk.payload.signature"
    path = write_json(
        tmp_path,
        "raw",
        {
            "access_token": raw_token,
            "message": f"buyer MST 1234567890 token {raw_token}",
            "pdf_url": "https://files.example.test/invoice.pdf?signature=private-signature",
            "buyer": {"name": "Real Person", "email": "real@example.org", "tax_code": "1234567890"},
            "provider_account_id": "account-secret-id",
        },
    )
    saved = path.read_text(encoding="utf-8")
    assert "REDACTED" in saved
    assert raw_token not in saved
    assert "Real Person" not in saved
    assert "real@example.org" not in saved
    assert "1234567890" not in saved
    assert "account-secret-id" not in saved
    assert "private-signature" not in saved


def test_quota_404_is_absent(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return token(request)
        if request.url.path == "/v1/usage":
            return httpx.Response(
                404, json={"success": False, "error": {"code": "NOT_FOUND", "message": "Not found"}}
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s6_quota(probe)
    assert result["scenario"] == "ABSENT"
    assert result["quota_endpoint"] is False


def test_usage_quota_string_and_server_error_status() -> None:
    status = 200

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return token(request)
        if request.url.path == "/v1/usage":
            if status == 200:
                return httpx.Response(
                    200, json={"success": True, "data": {"quota_remaining": "534"}}
                )
            return httpx.Response(
                503,
                json={"success": False, "error": {"code": "TEMPORARY", "message": "Retry later"}},
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s6_quota(probe)
    assert result["scenario"] == "PASS"
    assert result["quota_endpoint"] is True
    assert result["body"]["data"]["quota_remaining"] == "534"

    status = 503
    with client_with(handler) as probe:
        result = s6_quota(probe)
    assert result["scenario"] == "INCONCLUSIVE"
    assert result["quota_endpoint"] is None


def test_cli_validates_datetime_without_rewriting_value() -> None:
    import argparse

    from tools.einvoice_probe.__main__ import _issued_date

    assert _issued_date(ISSUED) == ISSUED
    try:
        _issued_date("2026-09-24")
    except argparse.ArgumentTypeError as error:
        assert "YYYY-MM-DD HH:mm:ss" in str(error)
    else:
        raise AssertionError("date-only input should be rejected")


def s3_handler(invalid_create: httpx.Response, invalid_state: str | None = None):
    """Control create succeeds; the invalid-MST create answers as given."""
    creates = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "control"}}
                )
            return invalid_create
        if request.url.path.endswith("/control"):
            return check_response("Success")
        if invalid_state and request.url.path.endswith("/invalid"):
            return check_response(invalid_state)
        raise AssertionError(request.url.path)

    return handler


def test_s3_invalid_create_ending_success_is_not_triggerable() -> None:
    clock = FakeClock()
    accepted = httpx.Response(200, json={"success": True, "data": {"tracking_code": "invalid"}})
    with client_with(s3_handler(accepted, "Success")) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate=10, issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["variant"] == "not_triggerable"
    assert result["reason"] == "created_success"
    assert result["retry"] is None
    assert result["scenario"] == "INCONCLUSIVE"


def test_s3_sync_reject_400_is_sync_reject() -> None:
    rejected = httpx.Response(
        400, json={"success": False, "error": {"code": "INVALID_TAX_CODE", "message": "Bad"}}
    )
    with client_with(s3_handler(rejected)) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["variant"] == "sync_reject"
    assert result["reason"] is None
    assert result["create_status"] == 400
    assert result["scenario"] == "INCONCLUSIVE"


@pytest.mark.parametrize("duplicate_status", [400, 422])
def test_s2_duplicate_rejected_without_409_is_fail(duplicate_status: int) -> None:
    creates = 0
    reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates, reference
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            if creates == 1:
                reference = json.loads(request.content)["reference_code"]
                return httpx.Response(
                    200, json={"success": True, "data": {"tracking_code": "track"}}
                )
            return httpx.Response(
                duplicate_status,
                json={"success": False, "error": {"code": "INVALID", "message": "Rejected"}},
            )
        if request.url.path.endswith("/track"):
            return check_response("Success")
        if request.url.path == f"/v1/invoices/{reference}":
            return httpx.Response(
                200, json={"success": True, "data": {"reference_code": reference}}
            )
        if request.url.path == "/v1/invoices":
            return httpx.Response(
                200, json={"success": True, "data": paging([{"reference_code": reference}])}
            )
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["first_state"] == "Success"
    assert any(item["matched"] for item in result["lookups"])
    assert result["duplicate"]["status"] == duplicate_status
    assert result["scenario"] == "FAIL"


def s4_handler(download):
    """s4 issue succeeds with tracking code "track"; downloads answer via `download`."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "track"}})
        if request.url.path.endswith("/create/check/track"):
            return check_response("Success")
        if request.url.path.endswith("/download"):
            paths.append(request.url.path)
            return download(request)
        raise AssertionError(request.url.path)

    return handler, paths


def with_content(request: httpx.Request, content: str) -> httpx.Response:
    body = download_response(request).json()
    body["data"]["content"] = content
    return httpx.Response(200, json=body)


def test_s4_empty_xml_content_is_fail() -> None:
    def download(request: httpx.Request) -> httpx.Response:
        if request.url.params["type"] == "xml":
            return with_content(request, "")
        return download_response(request)

    handler, paths = s4_handler(download)
    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["issue"]["scenario"] == "PASS"
    assert result["downloads"]["pdf"]["size"] > 0
    assert result["downloads"]["xml"]["status"] == 200
    assert result["downloads"]["xml"]["size"] == 0
    assert result["downloads"]["xml"]["sha256"] is None
    assert result["scenario"] == "FAIL"
    assert paths == ["/v1/invoices/track/download"] * 2


def test_s4_accepts_mime_wrapped_base64_and_rejects_undecodable() -> None:
    xml = b"<invoice>" + b"sandbox-line " * 20 + b"</invoice>"
    encoded = base64.encodebytes(xml).decode()
    assert "\n" in encoded

    def download(request: httpx.Request) -> httpx.Response:
        if request.url.params["type"] == "xml":
            return with_content(request, encoded)
        return download_response(request)

    handler, _ = s4_handler(download)
    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["downloads"]["xml"]["size"] == len(xml)
    assert result["downloads"]["xml"]["sha256"] == hashlib.sha256(xml).hexdigest()
    assert result["scenario"] == "PASS"

    for garbage in ("%%%%", "abc"):
        handler, _ = s4_handler(
            lambda request, garbage=garbage: (
                with_content(request, garbage)
                if request.url.params["type"] == "pdf"
                else download_response(request)
            )
        )
        with client_with(handler) as probe:
            result = s4_download(
                probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
            )
        assert result["downloads"]["pdf"]["size"] == 0, garbage
        assert result["scenario"] == "FAIL", garbage


def test_s4_download_falls_back_to_reference_code_on_404() -> None:
    def download(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/invoices/track/download":
            return httpx.Response(
                404, json={"success": False, "error": {"code": "NOT_FOUND", "message": "No"}}
            )
        return download_response(request)

    handler, paths = s4_handler(download)
    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    reference = result["issue"]["reference_code"]
    assert (
        paths
        == [
            "/v1/invoices/track/download",
            f"/v1/invoices/{reference}/download",
        ]
        * 2
    )
    for item in result["downloads"].values():
        assert item["identifier"] == "reference_code"
        assert item["attempts"] == [
            {"identifier": "tracking_code", "status": 404},
            {"identifier": "reference_code", "status": 200},
        ]
    assert result["scenario"] == "PASS"


def test_s4_download_does_not_fall_back_on_non_404() -> None:
    def download(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"success": False, "error": {"code": "BAD_REQUEST", "message": "type"}}
        )

    handler, paths = s4_handler(download)
    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert paths == ["/v1/invoices/track/download"] * 2
    for item in result["downloads"].values():
        assert item["identifier"] == "tracking_code"
        assert item["attempts"] == [{"identifier": "tracking_code", "status": 400}]
    assert result["scenario"] == "FAIL"


def test_s4_skips_download_without_tracking_code() -> None:
    downloads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal downloads
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(
                400, json={"success": False, "error": {"code": "BAD_REQUEST", "message": "No"}}
            )
        if request.url.path.endswith("/download"):
            downloads += 1
            return download_response(request)
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s4_download(probe, tax_rate=10, issued_date=ISSUED)
    assert downloads == 0
    for item in result["downloads"].values():
        assert item["skipped"] == "missing_tracking_code"
        assert item["status"] is None
        assert item["attempts"] == []
    assert result["scenario"] == "FAIL"


def test_docs_shaped_account_selects_template_by_code_only() -> None:
    two_templates = {
        "success": True,
        "data": {
            "id": "account-1",
            "templates": [
                {"template_code": "2", "invoice_series": "C26TMA"},
                {"template_code": "1", "invoice_series": "C26TSE"},
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return token(request)
        if request.url.path == "/v1/provider-accounts":
            return httpx.Response(200, json=LIST_BODY)
        if request.url.path == "/v1/provider-accounts/account-1":
            return httpx.Response(200, json=two_templates)
        raise AssertionError(request.url.path)

    from tools.einvoice_probe.scenarios import select_provider_account

    with client_with(handler) as probe:
        selected = select_provider_account(accounts(probe), probe, template_code="1")
    assert selected == {
        "provider_account_id": "account-1",
        "template_code": "1",
        "invoice_series": "C26TSE",
    }


def test_inactive_account_is_skipped() -> None:
    from tools.einvoice_probe.scenarios import select_provider_account

    rows = [
        {"id": "inactive", "active": False, "template_code": "1", "invoice_series": "C26TOLD"},
        {"id": "active", "active": True, "template_code": "1", "invoice_series": "C26TSE"},
    ]
    selected = select_provider_account({"body": {"data": {"items": rows, "paging": PAGING}}})
    assert selected["provider_account_id"] == "active"
    assert selected["invoice_series"] == "C26TSE"


def test_list_requests_use_documented_max_per_page() -> None:
    per_page: dict[str, str] = {}
    reference = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reference
        if request.url.path in {"/v1/provider-accounts", "/v1/invoices"}:
            per_page[request.url.path] = request.url.params["per_page"]
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            reference = json.loads(request.content)["reference_code"]
            return httpx.Response(200, json={"success": True, "data": {"tracking_code": "track"}})
        if request.url.path.endswith("/track"):
            return check_response("Success")
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": paging([])})
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(404, json={"success": False})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        s2_duplicate_reference(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert per_page == {"/v1/provider-accounts": "100", "/v1/invoices": "100"}


def test_token_is_cached_across_requests() -> None:
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path == "/v1/token":
            token_requests += 1
            return token(request)
        assert request.headers["Authorization"] == "Bearer mock.jwt.token"
        return httpx.Response(200, json=LIST_BODY)

    with client_with(handler) as probe:
        probe.request_json("GET", "/v1/provider-accounts")
        probe.request_json("GET", "/v1/provider-accounts")
    assert token_requests == 1


def test_requests_are_paced_half_a_second_apart(pace_clock) -> None:
    sent_at: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_at.append(pace_clock.now)
        if request.url.path == "/v1/token":
            return token(request)
        return httpx.Response(200, json=LIST_BODY)

    with client_with(handler) as probe:
        probe.request_json("GET", "/v1/provider-accounts")
        probe.request_json("GET", "/v1/provider-accounts")
    assert len(sent_at) == 3
    assert all(later - earlier >= 0.5 for earlier, later in pairwise(sent_at))
    assert pace_clock.sleeps == [0.5, 0.5]


def test_cli_requires_issued_date(tmp_path: Path, monkeypatch, capsys) -> None:
    import tools.einvoice_probe.__main__ as cli

    env_file = tmp_path / "probe.env"
    env_file.write_text(
        "SEPAY_EINVOICE_ENVIRONMENT=test\n"
        "SEPAY_EINVOICE_SANDBOX_CLIENT_ID=offline-id\n"
        "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET=offline-secret\n",
        encoding="utf-8",
    )

    def no_network(*_args, **_kwargs):
        raise AssertionError("the CLI built a client without --issued-date")

    monkeypatch.setattr(cli, "ProbeClient", no_network)
    monkeypatch.setattr(
        "sys.argv",
        [
            "einvoice_probe",
            "s1",
            "--env",
            str(env_file),
            "--out",
            str(tmp_path),
            "--tax-rate",
            "10",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    assert "--issued-date is required" in capsys.readouterr().err


# Each value lax-decodes to bytes once non-base64 characters are dropped; lengths cover mod 4.
MISPLACED_CONTENT = [
    "https://files.example.test/invoice.pdf?v=1",
    "https://files.example.test/invoice-.pdf?v=1",
    "https://files.example.test/invoice--.pdf?v=1",
    "https://files.example.test/invoice---.pdf?v=1",
    "<invoice>x</invoice>",
    "<invoice>x-</invoice>",
    "<invoice>x--</invoice>",
    "<invoice>x---</invoice>",
]


def test_misplaced_content_covers_every_length_mod_four() -> None:
    urls = [value for value in MISPLACED_CONTENT if value.startswith("https://")]
    xmls = [value for value in MISPLACED_CONTENT if value.startswith("<invoice>")]
    assert sorted(len(value) % 4 for value in urls) == [0, 1, 2, 3]
    assert sorted(len(value) % 4 for value in xmls) == [0, 1, 2, 3]


@pytest.mark.parametrize("content", MISPLACED_CONTENT)
def test_s4_url_or_raw_xml_content_is_fail(content: str) -> None:
    assert base64.b64decode(content), "lax decoding must yield bytes for this case to matter"

    def download(request: httpx.Request) -> httpx.Response:
        if request.url.params["type"] == "xml":
            return with_content(request, content)
        return download_response(request)

    handler, _ = s4_handler(download)
    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["issue"]["scenario"] == "PASS"
    assert result["downloads"]["pdf"]["size"] > 0
    assert result["downloads"]["xml"]["status"] == 200
    assert result["downloads"]["xml"]["size"] == 0
    assert result["downloads"]["xml"]["sha256"] is None
    assert result["scenario"] == "FAIL"


def test_s4_strips_only_crlf_before_strict_decoding() -> None:
    xml = b"<invoice>" + b"sandbox-line " * 20 + b"</invoice>"
    crlf = base64.encodebytes(xml).decode().replace("\n", "\r\n")
    spaced = base64.encodebytes(xml).decode().replace("\n", " ")

    def run(content: str) -> dict:
        handler, _ = s4_handler(
            lambda request: (
                with_content(request, content)
                if request.url.params["type"] == "xml"
                else download_response(request)
            )
        )
        with client_with(handler) as probe:
            return s4_download(
                probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
            )

    wrapped = run(crlf)
    assert wrapped["downloads"]["xml"]["sha256"] == hashlib.sha256(xml).hexdigest()
    assert wrapped["scenario"] == "PASS"
    unwrapped = run(spaced)
    assert unwrapped["downloads"]["xml"]["size"] == 0
    assert unwrapped["scenario"] == "FAIL"


def test_s1_without_polls_is_inconclusive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(200, json={"success": True, "data": {}})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s1_create_poll(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert result["create"]["status"] == 200
    assert result["tracking_code"] is None
    assert result["polls"] == []
    assert result["scenario"] == "INCONCLUSIVE"


def test_s3_check_404_on_invalid_create_is_not_triggerable() -> None:
    accepted = httpx.Response(200, json={"success": True, "data": {"tracking_code": "invalid"}})
    base = s3_handler(accepted)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/create/check/invalid"):
            return httpx.Response(
                404, json={"success": False, "error": {"code": "NOT_FOUND", "message": "No"}}
            )
        return base(request)

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    assert [poll["status"] for poll in result["polls"]] == [404]
    assert result["poll_exhausted"] is False
    assert result["variant"] == "not_triggerable"
    assert result["reason"] == "failed_create_or_poll_missing_tracking"
    assert result["retry"] is None
    assert result["scenario"] == "INCONCLUSIVE"


@pytest.mark.parametrize("file_name", [None, ""])
def test_s4_missing_file_name_is_fail(file_name) -> None:
    def download(request: httpx.Request) -> httpx.Response:
        body = download_response(request).json()
        if request.url.params["type"] == "pdf":
            body["data"]["file_name"] = file_name
        return httpx.Response(200, json=body)

    handler, _ = s4_handler(download)
    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate=10, issued_date=ISSUED, sleep=FakeClock().sleep, clock=FakeClock()
        )
    pdf = result["downloads"]["pdf"]
    assert result["issue"]["scenario"] == "PASS"
    assert (pdf["status"], pdf["file_type"]) == (200, "pdf")
    assert pdf["size"] > 0
    assert pdf["file_name"] == file_name
    assert result["scenario"] == "FAIL"


def test_non_json_response_body_keeps_only_type_and_size() -> None:
    raw = b"%PDF-1.4 sandbox bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/token":
            return token(request)
        return httpx.Response(200, content=raw, headers={"content-type": "application/pdf"})

    with client_with(handler) as probe:
        status, body, headers = probe.request_json("GET", "/v1/invoices/track/download")
    assert status == 200
    assert body == {"content_type": "application/pdf", "size": len(raw)}
    assert headers["content-type"] == "application/pdf"


def cli_env(tmp_path: Path, environment: str = "test") -> Path:
    env_file = tmp_path / "probe.env"
    env_file.write_text(
        f"SEPAY_EINVOICE_ENVIRONMENT={environment}\n"
        "SEPAY_EINVOICE_SANDBOX_CLIENT_ID=offline-id\n"
        "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET=offline-secret\n",
        encoding="utf-8",
    )
    return env_file


class OfflineClient:
    """Stands in for ProbeClient so CLI tests never build an HTTP client."""

    def __init__(self, env) -> None:
        self.env = env

    def __enter__(self) -> "OfflineClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def no_network(*_args, **_kwargs):
    raise AssertionError("the CLI built a client")


def run_cli(monkeypatch, tmp_path: Path, *args: str, env_file: Path | None = None) -> int:
    import tools.einvoice_probe.__main__ as cli

    env_file = env_file or cli_env(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["einvoice_probe", args[0], "--env", str(env_file), "--out", str(tmp_path), *args[1:]],
    )
    return cli.main()


@pytest.mark.parametrize("command", ["s1", "s4"])
def test_cli_invoice_scenarios_require_tax_rate(
    command: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("tools.einvoice_probe.__main__.ProbeClient", no_network)
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, tmp_path, command, "--issued-date", ISSUED)
    assert exit_info.value.code == 2
    assert "--tax-rate is required" in capsys.readouterr().err


def test_cli_quota_runs_without_tax_rate_or_issued_date(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr("tools.einvoice_probe.__main__.ProbeClient", OfflineClient)
    monkeypatch.setattr(
        "tools.einvoice_probe.__main__.s6_quota",
        lambda client: calls.append(client) or {"scenario": "PASS"},
    )
    assert run_cli(monkeypatch, tmp_path, "s6") == 0
    assert len(calls) == 1
    assert json.loads((tmp_path / "s6.json").read_text(encoding="utf-8")) == {"scenario": "PASS"}


def test_cli_passes_integer_tax_rate_and_issued_date(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_s1(client, **kwargs):
        seen.update(kwargs)
        return {"scenario": "PASS"}

    monkeypatch.setattr("tools.einvoice_probe.__main__.ProbeClient", OfflineClient)
    monkeypatch.setattr("tools.einvoice_probe.__main__.s1_create_poll", fake_s1)
    code = run_cli(monkeypatch, tmp_path, "s1", "--tax-rate", "10", "--issued-date", ISSUED)
    assert code == 0
    assert type(seen["tax_rate"]) is int
    assert seen["tax_rate"] == 10
    assert seen["issued_date"] == ISSUED


def test_cli_malformed_issued_date_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tools.einvoice_probe.__main__.ProbeClient", no_network)
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, tmp_path, "s1", "--tax-rate", "10", "--issued-date", "2026-09-24")
    assert exit_info.value.code == 2
    assert "YYYY-MM-DD HH:mm:ss" in capsys.readouterr().err


def test_cli_preflight_rejects_non_test_environment(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tools.einvoice_probe.__main__.ProbeClient", no_network)
    env_file = cli_env(tmp_path, environment="production")
    assert run_cli(monkeypatch, tmp_path, "preflight", env_file=env_file) == 2
    output = capsys.readouterr().out
    assert "environment: production" in output
    assert "offline-secret" not in output
