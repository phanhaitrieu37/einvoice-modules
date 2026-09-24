import base64
import inspect
import json
from datetime import date

import httpx

from tools.einvoice_probe.candidates import CANDIDATES
from tools.einvoice_probe.client import SANDBOX_BASE_URL, ProbeClient
from tools.einvoice_probe.scenarios import (
    evidence,
    s1_create_poll,
    s2_duplicate_reference,
    s3_failed_then_retry,
)

ENV = {
    "SEPAY_EINVOICE_ENVIRONMENT": "test",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "probe-client",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "probe-secret",
}
ACCOUNT_BODY = {
    "success": True,
    "data": [
        {
            "id": "account-1",
            "provider_account_id": "account-1",
            "active": True,
            "template_code": "1",
            "invoice_series": "C26TAA",
        }
    ],
}
ISSUED = date(2026, 9, 24)


def make_client(handler):
    return ProbeClient(ENV, httpx.MockTransport(handler))


def token_response(request):
    assert request.url.path == "/v1/token"
    assert (
        request.headers["Authorization"]
        == "Basic " + base64.b64encode(b"probe-client:probe-secret").decode()
    )
    return httpx.Response(
        200, json={"success": True, "data": {"access_token": "mock-token", "expires_in": 3600}}
    )


def account_response(request):
    if request.url.path == "/v1/token":
        return token_response(request)
    if request.url.path == "/v1/provider-accounts":
        assert request.headers["Authorization"] == "Bearer mock-token"
        return httpx.Response(200, json=ACCOUNT_BODY)
    return None


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
    with make_client(lambda _: httpx.Response(200)) as client:
        try:
            client.request_json("GET", "https://example.com/", authenticated=False)
        except ValueError as error:
            assert "sandbox" in str(error)
        else:
            raise AssertionError("non-sandbox host was accepted")


def test_token_never_written_to_output(tmp_path, monkeypatch, capsys, caplog) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if request.url.path == "/v1/token":
            return token_response(request)
        return httpx.Response(200, json={"success": True, "data": {"items": []}})

    with make_client(handler) as client:
        from tools.einvoice_probe.scenarios import write_json

        status, body, _ = client.request_json("GET", "/v1/provider-accounts")
        write_json(tmp_path, "result", {"access_token": body.get("token"), "status": status})
    combined = capsys.readouterr().out + (tmp_path / "result.json").read_text() + caplog.text
    assert received[1].headers["Authorization"] == "Bearer mock-token"
    assert "mock-token" not in combined
    assert "probe-secret" not in combined
    assert "mock-token" not in caplog.text
    assert "probe-secret" not in caplog.text


def test_s1_flow_with_mock_transport(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    creates: list[dict] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        account = account_response(request)
        if account:
            return account
        if request.url.path == "/v1/invoices/create":
            payload = json.loads(request.content)
            creates.append(payload)
            return httpx.Response(202, json={"success": True, "data": {"tracking_code": "trk-1"}})
        if request.url.path == "/v1/invoices/create/check/trk-1":
            poll_count += 1
            state = "Pending" if poll_count == 1 else "Success"
            return httpx.Response(200, json={"success": True, "data": {"status": state}})
        raise AssertionError(request.url.path)

    with make_client(handler) as client:
        result = s1_create_poll(client, tax_rate="10", issued_date=ISSUED)
    assert result["create"]["status"] == 202
    assert [item["status"] for item in result["polls"]] == [200, 200]
    assert result["polls"][-1]["body"]["data"]["status"] == "Success"
    assert creates[0]["template_code"] == "1"
    assert creates[0]["invoice_series"] == "C26TAA"
    assert creates[0]["issued_date"] == "2026-09-24"
    assert creates[0]["provider_account_id"] == "account-1"
    assert creates[0]["items"][0]["item_name"] == "Probe item"
    assert creates[0]["total_amount"] == 1100


def test_s2_records_409_and_lookup(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        account = account_response(request)
        if account:
            return account
        if request.method == "POST":
            count += 1
            status = 202 if count == 1 else 409
            return httpx.Response(status, json={"success": True, "data": {"tracking_code": "trk"}})
        return httpx.Response(200, json={"success": True, "data": {"reference_code": "ref"}})

    with make_client(handler) as client:
        result = s2_duplicate_reference(client, tax_rate="10", issued_date=ISSUED)
    assert result["first"]["status"] == 202
    assert result["duplicate"]["status"] == 409
    assert result["lookup"]["status"] == 200


def test_s3_sync_reject_is_inconclusive(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        account = account_response(request)
        if account:
            return account
        return httpx.Response(422, json={"success": False, "error": {"code": "INVALID"}})

    with make_client(handler) as client:
        result = s3_failed_then_retry(client, tax_rate="10", issued_date=ISSUED)
    assert result["variant"] == "sync_reject"
    assert result["retry"] is None


def test_evidence_file_shape(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    created = 0
    references: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        account = account_response(request)
        if account:
            return account
        if request.url.path == "/v1/invoices/create":
            created += 1
            payload = json.loads(request.content)
            reference = payload["reference_code"]
            references[reference] = references.get(reference, 0) + 1
            if references[reference] > 1:
                return httpx.Response(409, json={"success": False, "error": {"code": "EXISTS"}})
            return httpx.Response(
                202, json={"success": True, "data": {"tracking_code": f"trk-{created}"}}
            )
        if request.url.path.startswith("/v1/invoices/create/check/"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
        if request.url.path == "/v1/invoices/download":
            return httpx.Response(
                200, content=b"pdf-bytes", headers={"content-type": "application/pdf"}
            )
        if request.url.path == "/v1/quota":
            return httpx.Response(200, json={"success": True, "data": {"remaining": 3}})
        if request.url.path.startswith("/v1/invoices/") and request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": {"reference_code": "ref"}})
        if request.method == "POST":
            return httpx.Response(409, json={"success": False, "error": {"code": "EXISTS"}})
        raise AssertionError(request.url.path)

    with make_client(handler) as client:
        result = evidence(client, tax_rate="10", issued_date=ISSUED)
    assert result["scenarios"] == {
        "s1": "PASS",
        "s2": "PASS",
        "s3": "INCONCLUSIVE",
        "s4": "PASS",
        "s6": "PASS",
    }
    assert result["details"]["s4"]["download"]["sha256"]


def test_s4_download_404_marks_evidence_failed(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    created = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        account = account_response(request)
        if account:
            return account
        if request.url.path == "/v1/invoices/create":
            created += 1
            return httpx.Response(
                202, json={"success": True, "data": {"tracking_code": f"trk-{created}"}}
            )
        if request.url.path.startswith("/v1/invoices/create/check/"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
        if request.url.path == "/v1/invoices/download":
            return httpx.Response(404, json={"success": False, "error": {"code": "NOT_FOUND"}})
        if request.method == "POST":
            return httpx.Response(409, json={"success": False, "error": {"code": "EXISTS"}})
        if request.url.path == "/v1/quota":
            return httpx.Response(404, json={"success": False})
        return httpx.Response(200, json={"success": True, "data": {}})

    with make_client(handler) as client:
        result = evidence(client, tax_rate="10", issued_date=ISSUED)
    assert result["scenarios"]["s4"] == "FAIL"


def test_s3_async_failed_retries_same_reference_and_polls_success(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count
        account = account_response(request)
        if account:
            return account
        if request.url.path == "/v1/invoices/create":
            create_count += 1
            payload = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "success": True,
                    "data": {
                        "tracking_code": f"trk-{create_count}",
                        "reference_code": payload["reference_code"],
                    },
                },
            )
        if request.url.path.endswith("trk-1"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Failed"}})
        if request.url.path.endswith("trk-2"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
        raise AssertionError(request.url.path)

    with make_client(handler) as client:
        result = s3_failed_then_retry(client, tax_rate="10", issued_date=ISSUED)
    assert result["variant"] == "async_failed"
    assert result["retry"]["polls"][-1]["body"]["data"]["status"] == "Success"
    assert result["retry"]["create"]["status"] == 202


def test_s3_non_rejection_404_is_not_triggerable(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        account = account_response(request)
        if account:
            return account
        return httpx.Response(404, json={"success": False})

    with make_client(handler) as client:
        result = s3_failed_then_retry(client, tax_rate="10", issued_date=ISSUED)
    assert result["variant"] == "not_triggerable"
    assert result["create"]["status"] == 404


def test_s3_pending_then_failed_records_each_poll(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        account = account_response(request)
        if account:
            return account
        if request.url.path == "/v1/invoices/create":
            return httpx.Response(202, json={"success": True, "data": {"tracking_code": "trk-1"}})
        if request.url.path.endswith("trk-1"):
            count += 1
            state = "Pending" if count == 1 else "Failed"
            return httpx.Response(200, json={"success": True, "data": {"status": state}})
        raise AssertionError(request.url.path)

    with make_client(handler) as client:
        result = s3_failed_then_retry(client, tax_rate="10", issued_date=ISSUED)
    assert result["variant"] == "async_failed"
    assert [poll["body"]["data"]["status"] for poll in result["polls"]] == ["Pending", "Failed"]


def test_account_selection_rejects_zero_and_ambiguous_matches() -> None:
    from tools.einvoice_probe.scenarios import select_provider_account

    for body, count in [({"data": []}, 0), ({"data": ACCOUNT_BODY["data"] * 2}, 2)]:
        try:
            select_provider_account({"body": body})
        except RuntimeError as error:
            assert f"found {count}" in str(error)
        else:
            raise AssertionError("account selection should fail closed")


def test_account_template_selection_is_explicit() -> None:
    from tools.einvoice_probe.scenarios import select_provider_account

    response = {
        "body": {
            "data": [
                {
                    "id": "account-1",
                    "active": True,
                    "templates": [
                        {"template_code": "1", "invoice_series": "C26TSE"},
                        {"template_code": "2", "invoice_series": "C26TSE"},
                    ],
                }
            ]
        }
    }
    try:
        select_provider_account(response)
    except RuntimeError as error:
        assert "found 0" in str(error)
    else:
        raise AssertionError("ambiguous invoice template was selected implicitly")
    selected = select_provider_account(response, template_code="2")
    assert selected["template_code"] == "2"


def test_account_templates_are_loaded_from_documented_detail_endpoint(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/v1/token":
            return token_response(request)
        if request.url.path == "/v1/provider-accounts/account-1":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": "account-1",
                        "templates": [
                            {"template_code": "1", "invoice_series": "C26TSE"},
                            {"template_code": "2", "invoice_series": "C26TSE"},
                        ],
                    },
                },
            )
        raise AssertionError(request.url.path)

    from tools.einvoice_probe.scenarios import select_provider_account

    with make_client(handler) as client:
        selected = select_provider_account(
            {"body": {"data": [{"id": "account-1", "active": True}]}},
            client,
            template_code="2",
        )
    assert selected == {
        "provider_account_id": "account-1",
        "template_code": "2",
        "invoice_series": "C26TSE",
    }
    assert requests == ["/v1/token", "/v1/provider-accounts/account-1"]


def test_redact_camel_case_secret_keys() -> None:
    from tools.einvoice_probe.redact import redact

    assert redact({"accessToken": "hidden", "clientSecret": "hidden"}) == {
        "accessToken": "REDACTED",
        "clientSecret": "REDACTED",
    }
