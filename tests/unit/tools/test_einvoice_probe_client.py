import base64
import hashlib
import inspect
import json
from pathlib import Path

import httpx

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
LIST_BODY = {"success": True, "data": [{"id": "account-1", "active": True}]}
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
        json={"success": True, "data": {"access_token": "mock.jwt.token", "expires_in": 3600}},
    )


def setup_accounts(request):
    if request.url.path == "/v1/token":
        return token(request)
    if request.url.path == "/v1/provider-accounts":
        return httpx.Response(200, json=LIST_BODY)
    if request.url.path == "/v1/provider-accounts/account-1":
        return httpx.Response(200, json=DETAIL_BODY)
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
    assert CANDIDATES["provider_account_detail"]["path"] == "/v1/provider-accounts/{id}"
    assert "guess" in CANDIDATES["download"]["parameter_guess"]
    with client_with(lambda _: httpx.Response(200)) as client:
        try:
            client.request_json("GET", "https://example.com/", authenticated=False)
        except ValueError as error:
            assert "sandbox" in str(error)
        else:
            raise AssertionError("non-sandbox host was accepted")


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
        return result or httpx.Response(200, json={"success": True, "data": {"items": []}})

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

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates.append(json.loads(request.content))
            return httpx.Response(202, json={"success": True, "data": {"tracking_code": "track-1"}})
        if request.url.path.endswith("/track-1"):
            polls += 1
            state = "Pending" if polls == 1 else "Success"
            data = {"status": state}
            if state == "Success":
                data["invoice"] = {"pdf_url": "/files/p.pdf", "xml_url": "/files/p.xml"}
            return httpx.Response(200, json={"success": True, "data": data})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s1_create_poll(probe, tax_rate="10", issued_date=ISSUED, sleep=sleep, clock=clock)
    assert result["scenario"] == "PASS"
    assert [row["body"]["data"]["status"] for row in result["polls"]] == ["Pending", "Success"]
    payload = creates[0]
    assert payload["provider_account_id"] == "account-1"
    assert payload["template_code"] == "1"
    assert payload["invoice_series"] == "C26TSE"
    assert payload["issued_date"] == ISSUED
    assert payload["currency"] == "VND"
    assert payload["items"][0]["item_name"] == "Probe item"
    assert payload["total_amount"] == 1100
    assert sleeps == [2.5]


def test_s2_records_409_and_lookup(monkeypatch) -> None:
    clock = FakeClock()
    creates = 0
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        paths.append(request.url.path)
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            status = 202 if creates == 1 else 409
            return httpx.Response(status, json={"success": True, "data": {"tracking_code": "tr-1"}})
        if request.url.path.endswith("/tr-1"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(404, json={"success": False})
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": {"items": []}})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "PASS"
    assert result["first"]["status"] == 202
    assert result["first_polls"][-1]["body"]["data"]["status"] == "Success"
    assert result["duplicate"]["status"] == 409
    assert [item["status"] for item in result["lookups"]] == [404, 200]
    assert "/v1/invoices" in paths


def test_s2_pending_first_create_is_not_a_pass() -> None:
    clock = FakeClock()
    creates = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            creates += 1
            status = 202 if creates == 1 else 409
            return httpx.Response(
                status, json={"success": True, "data": {"tracking_code": "pending"}}
            )
        if request.url.path.endswith("/pending"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Pending"}})
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(404, json={"success": False})
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": {"items": []}})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s2_duplicate_reference(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["first_poll_exhausted"] is True
    assert result["first_state"] == "Pending"
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
                    202, json={"success": True, "data": {"tracking_code": "control"}}
                )
            return httpx.Response(422, json={"success": False, "error": {"code": "INVALID"}})
        if request.url.path.endswith("/control"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
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

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal track_count
        setup = setup_accounts(request)
        if setup:
            return setup
        if request.url.path == "/v1/invoices/create":
            payload = json.loads(request.content)
            reference = payload["reference_code"]
            created_by_reference[reference] = created_by_reference.get(reference, 0) + 1
            if created_by_reference[reference] > 1:
                return httpx.Response(409, json={"success": False, "error": {"code": "EXISTS"}})
            track_count += 1
            return httpx.Response(
                202, json={"success": True, "data": {"tracking_code": f"track-{track_count}"}}
            )
        if "/create/check/" in request.url.path:
            tracking = request.url.path.rsplit("/", 1)[-1]
            state = "Failed" if tracking == "track-3" else "Success"
            invoice = {"pdf_url": "/files/probe.pdf", "xml_url": "/files/probe.xml"}
            data = {"status": state, "invoice": invoice}
            return httpx.Response(200, json={"success": True, "data": data})
        if request.url.path.startswith("/files/"):
            return httpx.Response(
                200, content=request.url.path.rsplit(".", 1)[-1].encode() + b"-data"
            )
        if request.url.path == "/v1/quota":
            return httpx.Response(200, json={"success": True, "data": {"remaining": 2}})
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(200, json={"success": True, "data": {"invoice": {}}})
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": {"items": []}})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = evidence(probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock)
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
    assert all(
        item["sha256"] == hashlib.sha256(f"{kind}-data".encode()).hexdigest()
        for kind, item in result["details"]["s4"]["downloads"].items()
    )


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
                202,
                json={"success": True, "data": {"tracking_code": tracking}},
            )
        if "/create/check/" in request.url.path:
            tracking = request.url.path.rsplit("/", 1)[-1]
            state = "Failed" if tracking == "evidence-track-3" else "Success"
            if state == "Failed":
                failed_references.add(reference_by_track[tracking])
            invoice = {"pdf_url": "/files/a.pdf", "xml_url": "/files/a.xml"}
            return httpx.Response(
                200, json={"success": True, "data": {"status": state, "invoice": invoice}}
            )
        if request.url.path.startswith("/files/"):
            return httpx.Response(200, content=b"artifact")
        if request.url.path == "/v1/quota":
            return httpx.Response(200, json={"success": True, "data": {}})
        if request.url.path.startswith("/v1/invoices/"):
            return httpx.Response(200, json={"success": True, "data": {}})
        if request.url.path == "/v1/invoices":
            return httpx.Response(200, json={"success": True, "data": {"items": []}})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = evidence(probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock)
    assert result["scenarios"]["s3"] == "PASS"
    assert result["details"]["s3"]["retry_create_status"] == 202


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
                202, json={"success": True, "data": {"tracking_code": f"track-{created}"}}
            )
        if "/create/check/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "status": "Success",
                        "invoice": {"pdf_url": "/files/p.pdf", "xml_url": "/files/p.xml"},
                    },
                },
            )
        if request.url.path.startswith("/files/"):
            return httpx.Response(404, json={"success": False})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s4_download(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "FAIL"
    assert result["downloads"]["pdf"]["status"] == 404
    assert result["downloads"]["xml"]["status"] == 404


def test_artifact_urls_are_fetched_without_forwarding_bearer_token() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, content=b"pdf artifact")

    from tools.einvoice_probe.scenarios import _download_artifact

    with client_with(handler) as probe:
        artifact = _download_artifact(
            probe,
            "https://files.example.test/invoice.pdf?format=pdf",
            "tracking-code",
            "pdf",
        )
    assert artifact["status"] == 200
    assert artifact["sha256"] == hashlib.sha256(b"pdf artifact").hexdigest()
    assert observed[0].url.host == "files.example.test"
    assert "Authorization" not in observed[0].headers
    assert artifact["url"] == "https://files.example.test/invoice.pdf"


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
                    202, json={"success": True, "data": {"tracking_code": "control"}}
                )
            if create_count == 2:
                return httpx.Response(
                    202, json={"success": True, "data": {"tracking_code": "failed"}}
                )
            return httpx.Response(202, json={"success": True, "data": {"tracking_code": "retry"}})
        if request.url.path.endswith("/control"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
        if request.url.path.endswith("/failed"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Failed"}})
        if request.url.path.endswith("/retry"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
        raise AssertionError(request.url.path)

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["scenario"] == "PASS"
    assert result["retry_create_status"] == 202
    assert create_payloads[1]["reference_code"] == create_payloads[2]["reference_code"]
    assert "tax_code" in create_payloads[1]["buyer"]
    assert "tax_code" not in create_payloads[2]["buyer"]


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
            status = 202 if creates == 1 else 404
            return httpx.Response(
                status, json={"success": True, "data": {"tracking_code": "control"}}
            )
        if request.url.path.endswith("/control"):
            return httpx.Response(200, json={"success": True, "data": {"status": "Success"}})
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
            return httpx.Response(202, json={"success": True, "data": {"tracking_code": "control"}})
        if request.url.path.endswith("/control"):
            polls += 1
            return httpx.Response(200, json={"success": True, "data": {"status": "Pending"}})
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
                202,
                json={"success": True, "data": {"tracking_code": f"track-{create_count}"}},
            )
        return httpx.Response(200, json={"success": True, "data": {"status": "Pending"}})

    with client_with(handler) as probe:
        result = s3_failed_then_retry(
            probe, tax_rate="10", issued_date=ISSUED, sleep=clock.sleep, clock=clock
        )
    assert result["poll_exhausted"] is True
    assert result["reason"] == "pending_at_bound"


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
            return httpx.Response(202, json={"success": True, "data": {"tracking_code": code}})
        if request.url.path.endswith("failed-track"):
            poll_count += 1
            state = "Pending" if poll_count == 1 else "Failed"
            return httpx.Response(200, json={"success": True, "data": {"status": state}})
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
                "create": {"status": 202},
                "polls": [{"status": 200, "body": {"data": {"status": "Failed"}}}],
            }
        )
        == "FAIL"
    )
    assert (
        _success(
            {
                "create": {"status": 202},
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
            "create": {"status": 202},
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
            "create": {"status": 202},
            "polls": [{"body": {"data": {"status": "Pending"}}}],
            "poll_exhausted": True,
        },
    )
    result = scenarios.evidence(object(), tax_rate="10", issued_date=ISSUED)
    assert result["scenarios"]["s1"] == "INCONCLUSIVE"


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
        return httpx.Response(404, json={"success": False})

    with client_with(handler) as probe:
        result = s6_quota(probe)
    assert result["scenario"] == "ABSENT"
    assert result["quota_endpoint"] is False


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
