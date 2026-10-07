import base64
import inspect
import json
from pathlib import Path

import httpx
import pytest

from tools.einvoice_probe.client import ProbeClient
from tools.einvoice_probe.scenarios import (
    _invoice_payload,
    _issue_payload,
    record_call,
    s1_create_poll,
    s2_duplicate_reference,
    s3_failed_then_retry,
    tax_matrix,
)

ENV = {
    "SEPAY_EINVOICE_ENVIRONMENT": "test",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "probe-client",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "probe-secret",
}
ISSUED = "2026-10-07 10:00:00"
ACCOUNT = {"provider_account_id": "account-1", "template_code": "1", "invoice_series": "C26TSE"}
ACCOUNT_LIST = {
    "success": True,
    "data": {"items": [{"id": "account-1", "active": True, "provider": "matbao"}]},
}
ACCOUNT_DETAIL = {
    "success": True,
    "data": {
        "id": "account-1",
        "provider": "matbao",
        "templates": [{"template_code": "1", "invoice_series": "C26TSE"}],
    },
}


def token_or_account(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/v1/token":
        expected = "Basic " + base64.b64encode(b"probe-client:probe-secret").decode()
        assert request.headers["Authorization"] == expected
        return httpx.Response(
            200, json={"success": True, "data": {"access_token": "mock.jwt.token"}}
        )
    if request.url.path == "/v1/provider-accounts":
        return httpx.Response(200, json=ACCOUNT_LIST)
    if request.url.path == "/v1/provider-accounts/account-1":
        return httpx.Response(200, json=ACCOUNT_DETAIL)
    return None


def test_draft_payload_sets_is_draft_true() -> None:
    parameter = inspect.signature(_invoice_payload).parameters["is_draft"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        _invoice_payload("ref-1", 8, ISSUED, ACCOUNT)  # type: ignore[call-arg]
    draft = _invoice_payload("ref-1", 8, ISSUED, ACCOUNT, is_draft=True)
    issued = _invoice_payload("ref-1", 8, ISSUED, ACCOUNT, is_draft=False)
    assert draft["is_draft"] is True
    assert issued["is_draft"] is False
    assert {key: value for key, value in draft.items() if key != "is_draft"} == {
        key: value for key, value in issued.items() if key != "is_draft"
    }


def test_existing_scenarios_still_send_is_draft_false() -> None:
    create_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        setup = token_or_account(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            body = json.loads(request.content)
            create_bodies.append(body)
            return httpx.Response(
                202, json={"success": True, "data": {"tracking_code": body["reference_code"]}}
            )
        if request.url.path.startswith("/v1/invoices/create/check/"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
        if request.url.path.startswith("/v1/invoices"):
            return httpx.Response(404, json={"success": False})
        raise AssertionError(request.url.path)

    with ProbeClient(ENV, httpx.MockTransport(handler)) as probe:
        s1_create_poll(probe, tax_rate=8, issued_date=ISSUED, account=ACCOUNT)
        s2_duplicate_reference(probe, tax_rate=8, issued_date=ISSUED, account=ACCOUNT)
        s3_failed_then_retry(probe, tax_rate=8, issued_date=ISSUED, account=ACCOUNT)
        tax_matrix(probe, issued_date=ISSUED, account=ACCOUNT)
    # s1: 1, s2: 2, s3: control + invalid, tax matrix: 6.
    assert len(create_bodies) == 11
    assert all(body["is_draft"] is False for body in create_bodies)


def test_issue_payload_is_reference_only() -> None:
    assert _issue_payload("3f1c0d1e-0000-4000-8000-000000000001") == {
        "reference_code": "3f1c0d1e-0000-4000-8000-000000000001"
    }


def test_record_call_writes_redacted_raw_with_classifier_paths(tmp_path: Path) -> None:
    response = {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": {
            "access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
            "data": {
                "status": "Success",
                "provider_account_id": "12345678-1234-1234-1234-123456789abc",
                "invoice": {
                    "status": "draft",
                    "buyer": {"name": "Real Person", "email": "real@person.vn"},
                    "pdf_url": "https://portal.test/x.pdf?key=private",
                },
            },
        },
    }
    paths = {"job_state": "body.data.status", "draft_marker": "body.data.invoice.status"}
    record = record_call(tmp_path, "draft_check", response, paths)
    assert record["classifier_paths"] == paths
    assert record["body"]["data"]["invoice"]["status"] == "draft"
    written = json.loads((tmp_path / "draft_check.json").read_text(encoding="utf-8"))
    assert written["classifier_paths"] == paths
    assert written["status"] == 200
    assert written["body"]["access_token"] == "REDACTED"
    assert written["body"]["data"]["provider_account_id"] == "00000000-0000-0000-0000-000000000000"
    assert written["body"]["data"]["invoice"]["buyer"] == {
        "name": "REDACTED",
        "email": "REDACTED",
    }
    assert written["body"]["data"]["invoice"]["pdf_url"] == "https://portal.test/x.pdf?REDACTED"
    assert written["body"]["data"]["invoice"]["status"] == "draft"
    assert "private" not in (tmp_path / "draft_check.json").read_text(encoding="utf-8")
    assert record_call(None, "not_written", response, paths)["classifier_paths"] == paths
    assert not (tmp_path / "not_written.json").exists()
