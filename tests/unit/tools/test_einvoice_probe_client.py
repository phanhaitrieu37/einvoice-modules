import inspect
import json

import httpx

from tools.einvoice_probe.client import SANDBOX_BASE_URL, ProbeClient
from tools.einvoice_probe.scenarios import (
    evidence,
    s1_create_poll,
    s2_duplicate_reference,
    s3_failed_then_retry,
)

ENV = {
    "SEPAY_EINVOICE_ENVIRONMENT": "test",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "client-secret-value",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "private-secret-value",
}


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

    with ProbeClient(ENV, httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        try:
            client.request_json("GET", "https://example.com/", authenticated=False)
        except ValueError as error:
            assert "sandbox" in str(error)
        else:
            raise AssertionError("non-sandbox host was accepted")


def test_token_never_written_to_output(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "eyJabcdefghijk.payload.signature", "expires_in": 3600}
            )
        return httpx.Response(200, json={"ok": True})

    with ProbeClient(ENV, httpx.MockTransport(handler)) as client:
        result = client.request_json("GET", "/v1/provider-accounts")
        from tools.einvoice_probe.scenarios import write_json

        write_json(
            tmp_path,
            "result",
            {"result": result, "client_secret": ENV["SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET"]},
        )
    output = capsys.readouterr().out
    written = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "eyJ" not in output + written
    assert "private-secret-value" not in output + written


def test_s1_flow_with_mock_transport(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path == "/v1/invoices":
            body = json.loads(request.content)
            return httpx.Response(202, json={"tracking_code": body["reference_code"]})
        return httpx.Response(200, json={"status": "Pending"})

    with ProbeClient(ENV, httpx.MockTransport(handler)) as client:
        result = s1_create_poll(client, tax_rate=10)
    assert result["create"]["status"] == 202
    assert result["check"]["status"] == 200
    assert calls == [
        "/oauth/token",
        "/v1/invoices",
        "/v1/invoices/issue/check/" + result["reference_code"],
    ]


def test_s2_records_409_and_lookup(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "token"})
        if request.method == "POST":
            return httpx.Response(409, json={"code": "EXISTS"})
        return httpx.Response(200, json={"reference_code": request.url.path.rsplit("/", 1)[-1]})

    with ProbeClient(ENV, httpx.MockTransport(handler)) as client:
        result = s2_duplicate_reference(client, tax_rate=10)
    assert result["duplicate"]["status"] == 409
    assert result["lookup"]["status"] == 200


def test_s3_sync_reject_is_inconclusive(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(422, json={"message": "invalid tax code"})

    with ProbeClient(ENV, httpx.MockTransport(handler)) as client:
        result = s3_failed_then_retry(client, tax_rate=10)
    assert result["variant"] == "sync_reject"
    assert result["retry"] is None


def test_evidence_file_shape(monkeypatch) -> None:
    monkeypatch.setattr("tools.einvoice_probe.client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "token"})
        if request.method == "GET" and "/download" in request.url.path:
            return httpx.Response(200, content=b"pdf", headers={"content-type": "application/pdf"})
        if request.method == "GET" and "/quotas" in request.url.path:
            return httpx.Response(200, json={"remaining": 3})
        if request.method == "GET" and "/issue/check/" in request.url.path:
            return httpx.Response(200, json={"status": "Success"})
        if request.method == "GET":
            return httpx.Response(200, json={"status": "Success"})
        if request.method == "POST":
            return httpx.Response(409, json={"code": "EXISTS", "tracking_code": "t"})
        return httpx.Response(200, json={"tracking_code": "t"})

    with ProbeClient(ENV, httpx.MockTransport(handler)) as client:
        result = evidence(client, tax_rate=10)
    assert result["environment"] == "test"
    assert set(result["scenarios"]) == {"s1", "s2", "s3", "s4", "s6"}
    assert result["details"]["s3_variant"] in {"async_failed", "sync_reject", "not_triggerable"}
