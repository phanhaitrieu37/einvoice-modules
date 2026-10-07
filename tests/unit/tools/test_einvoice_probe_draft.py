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
    s7_draft_then_issue,
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


class FakeSepay:
    """Stateful sandbox stand-in shaped like the documented create/issue/check/lookup bodies."""

    def __init__(
        self,
        *,
        pending_checks: tuple[int, ...] = (),
        draft_quota_cost: int = 0,
        issue_quota_cost: int = 1,
        issue_tracking_is_reference: bool = True,
        resend_while_pending: str = "accepted",
        after_issued: str = "conflict",
        lookup_hides_number: frozenset[str] = frozenset(),
        issue_date_mode: str = "issue_day",
        issue_day: str = "2026-10-07",
        reject_issue_with_date: bool = False,
        unknown_code: str = "EINVOICE_DRAFT_NOT_FOUND",
    ) -> None:
        self.pending_checks = list(pending_checks)
        self.draft_quota_cost = draft_quota_cost
        self.issue_quota_cost = issue_quota_cost
        self.issue_tracking_is_reference = issue_tracking_is_reference
        self.resend_while_pending = resend_while_pending
        self.after_issued = after_issued
        self.lookup_hides_number = lookup_hides_number
        self.issue_date_mode = issue_date_mode
        self.issue_day = issue_day
        self.reject_issue_with_date = reject_issue_with_date
        self.unknown_code = unknown_code
        self.quota = 1000
        self.next_number = 307700
        self.invoices: dict[str, dict] = {}
        self.issue_jobs: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict | None]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        setup = token_or_account(request)
        if setup:
            return setup
        path = request.url.path
        if path == "/v1/usage":
            return ok({"quota_remaining": str(self.quota)})
        if path == "/v1/invoices/create" and request.method == "POST":
            return self.create(body)
        if path.startswith("/v1/invoices/create/check/"):
            return self.check_create(path.rsplit("/", 1)[1])
        if path.startswith("/v1/invoices/issue/check/"):
            return self.check_issue(path.rsplit("/", 1)[1])
        if path == "/v1/invoices/issue" and request.method == "POST":
            return self.issue(body)
        if path.startswith("/v1/invoices/") and request.method == "GET":
            return self.lookup(path.rsplit("/", 1)[1])
        raise AssertionError(f"unexpected request {request.method} {path}")

    def create(self, body: dict) -> httpx.Response:
        reference = body["reference_code"]
        if reference in self.invoices:
            return error(409, "EINVOICE_DOCUMENT_EXISTED")
        draft = body["is_draft"]
        self.invoices[reference] = {
            "status": "draft" if draft else "issued",
            "number": None if draft else self.number(),
            "issued_date": body["issued_date"][:10],
            "pending_left": 0,
            "tracking": None,
        }
        self.quota -= self.draft_quota_cost if draft else self.issue_quota_cost
        return httpx.Response(202, json={"success": True, "data": {"tracking_code": reference}})

    def issue(self, body: dict) -> httpx.Response:
        reference = body["reference_code"]
        invoice = self.invoices.get(reference)
        if invoice is None:
            return error(500, self.unknown_code)
        if self.reject_issue_with_date and "issued_date" in body:
            return error(422, "INVOICE_DATE_NOT_ALLOWED")
        if invoice["status"] == "issuing":
            if self.resend_while_pending == "accepted":
                return accepted_issue(invoice["tracking"])
            return error(409, "EINVOICE_ISSUE_IN_PROGRESS")
        if invoice["status"] == "issued":
            if self.after_issued == "renumber":
                invoice["number"] = self.number()
                return accepted_issue(invoice["tracking"])
            if self.after_issued == "accepted":
                return accepted_issue(invoice["tracking"])
            return error(409, "EINVOICE_DOCUMENT_ISSUED")
        tracking = reference if self.issue_tracking_is_reference else f"issue-{reference}"
        self.issue_jobs[tracking] = reference
        invoice["tracking"] = tracking
        invoice["pending_left"] = self.pending_checks.pop(0) if self.pending_checks else 0
        self.quota -= self.issue_quota_cost
        if self.issue_date_mode == "issue_day":
            invoice["issued_date"] = body.get("issued_date", self.issue_day)[:10]
        if invoice["pending_left"]:
            invoice["status"] = "issuing"
        else:
            self.complete(invoice)
        return accepted_issue(tracking)

    def complete(self, invoice: dict) -> None:
        invoice["status"] = "issued"
        invoice["number"] = invoice["number"] or self.number()

    def check_create(self, reference: str) -> httpx.Response:
        if reference not in self.invoices:
            return error(404, "NOT_FOUND")
        return ok({"reference_code": reference, "status": "Success", **self.invoice(reference)})

    def check_issue(self, tracking: str) -> httpx.Response:
        reference = self.issue_jobs.get(tracking)
        if reference is None:
            return error(404, "NOT_FOUND")
        invoice = self.invoices[reference]
        if invoice["pending_left"]:
            invoice["pending_left"] -= 1
            return ok({"reference_code": reference, "status": "Pending", "invoice": None})
        if invoice["status"] == "issuing":
            self.complete(invoice)
        return ok({"reference_code": reference, "status": "Success", **self.invoice(reference)})

    def lookup(self, reference: str) -> httpx.Response:
        if reference not in self.invoices:
            return error(404, "NOT_FOUND")
        shown = self.invoice(reference)["invoice"]
        if reference in self.lookup_hides_number:
            shown["invoice_number"] = None
        return ok(shown)

    def invoice(self, reference: str) -> dict:
        invoice = self.invoices[reference]
        status = "draft" if invoice["status"] == "issuing" else invoice["status"]
        return {
            "invoice": {
                "reference_code": reference,
                "status": status,
                "invoice_number": invoice["number"] if status == "issued" else "0",
                "issued_date": invoice["issued_date"],
                "buyer": {"name": "Probe Buyer", "email": "probe@example.com"},
            }
        }

    def number(self) -> str:
        self.next_number += 1
        return str(self.next_number)

    def issue_bodies(self) -> list[dict]:
        return [body for method, path, body in self.requests if path == "/v1/invoices/issue"]

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


def ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def error(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status, json={"success": False, "error": {"code": code, "message": "probe"}}
    )


def accepted_issue(tracking: str) -> httpx.Response:
    return ok({"tracking_code": tracking, "message": "Đã tạo yêu cầu xuất bán hóa đơn điện tử"})


@pytest.fixture
def references(monkeypatch) -> list[str]:
    """Deterministic uuid4 values in call order: R, L, then pending retries and unknown."""
    values = [f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 9)]
    queue = iter(values)
    monkeypatch.setattr("tools.einvoice_probe.scenarios.uuid4", lambda: next(queue))
    return values


def run_s7(fake: FakeSepay, out_dir: Path | None = None) -> dict:
    with ProbeClient(ENV, fake.transport()) as probe:
        return s7_draft_then_issue(
            probe, tax_rate=8, issued_date=ISSUED, account=ACCOUNT, out_dir=out_dir
        )


def calls(result: dict) -> list[str]:
    return [name for name in result["steps"] if "_poll_" not in name]


CANONICAL_FILES = (
    "draft_create_accepted",
    "draft_check",
    "draft_lookup",
    "draft_recreate",
    "issue_accepted",
    "issue_pending_probe",
    "issue_resend_pending",
    "issue_check_success",
    "issue_lookup_issued",
    "issue_after_issued",
    "issue_recovered_lookup",
    "issue_recovered_check",
    "issue_recovered_check_create",
    "issue_unknown_reference",
)


def test_s7_records_every_step_in_order(references, tmp_path: Path) -> None:
    fake = FakeSepay(pending_checks=(1,))
    result = run_s7(fake, tmp_path)
    assert "stopped" not in result
    assert calls(result) == [
        "quota_0",
        "draft_create_accepted",
        "quota_1",
        "draft_check",
        "quota_1_after_draft_check",
        "draft_lookup",
        "draft_recreate",
        "quota_2",
        "issue_attempt_1_accepted",
        "issue_attempt_1_pending_probe",
        "issue_attempt_1_resend",
        "issue_accepted",
        "issue_check_success",
        "quota_3",
        "issue_pending_probe",
        "issue_resend_pending",
        "issue_lookup_issued",
        "issue_after_issued",
        "issue_after_issued_lookup",
        "lost_draft_create",
        "lost_issue_status",
        "issue_recovered_lookup",
        "issue_recovered_check",
        "issue_recovered_check_create",
        "issue_unknown_reference",
    ]
    for name in CANONICAL_FILES:
        written = json.loads((tmp_path / f"{name}.json").read_text(encoding="utf-8"))
        assert written["classifier_paths"], name
    draft_check = json.loads((tmp_path / "draft_check.json").read_text(encoding="utf-8"))
    assert draft_check["classifier_paths"]["invoice_status"] == "body.data.invoice.status"
    assert draft_check["body"]["data"]["invoice"]["buyer"]["name"] == "REDACTED"
    reference = references[0]
    assert result["reference_code"] == reference
    assert result["draft_create_accepted"] is True
    assert result["draft_marker"] == {"path": "body.data.invoice.status", "value": "draft"}
    assert result["draft_marker_lookup"] == {"path": "body.data.status", "value": "draft"}
    assert result["draft_marker_differs_when_issued"] is True
    assert result["draft_recreate_behaviour"] == {
        "http_status": 409,
        "error_code": "EINVOICE_DOCUMENT_EXISTED",
        "path": "body.error.code",
    }
    assert result["issue_check_state"] == {"path": "body.data.status", "value": "Success"}
    assert result["provider_issued_date_path"] == {
        "check": "body.data.invoice.issued_date",
        "lookup": "body.data.issued_date",
    }
    assert result["issued_date_lookup"] == "2026-10-07"
    assert result["issue_endpoint_verified"] is True
    assert result["recover_numbers_by_reference"] is True
    assert result["unconfirmed_result_messages"] == []
    first_issue = fake.issue_bodies()[0]
    assert first_issue == {"reference_code": reference}
    created = [body for _, path, body in fake.requests if path == "/v1/invoices/create"]
    assert all(body["is_draft"] is True for body in created)
    assert set(result["classifier_paths"]) >= {
        "draft_create_accepted",
        "draft_marker",
        "draft_marker_lookup",
        "draft_recreate_behaviour",
        "draft_consumes_quota",
        "issue_endpoint_verified",
        "issue_tracking_equals_reference",
        "issue_resend_while_pending",
        "issue_after_issued",
        "issue_idempotent",
        "issue_unknown_reference",
        "issue_consumes_quota",
        "provider_issued_date_path",
        "recover_numbers_by_reference",
        "recover_after_lost_issue_response",
        "issue_check_state",
        "issued_lookup_has_numbers",
        "issue_pending_observed",
        "recover_by_check_reference",
    }


def test_s7_issue_resend_while_pending_recorded_with_identity(references) -> None:
    fake = FakeSepay(pending_checks=(2,), after_issued="accepted")
    result = run_s7(fake)
    reference = references[0]
    assert result["issue_pending_observed"] is True
    assert result["issue_pending_references_tried"] == 1
    assert result["issue_resend_reference"] == reference
    assert result["issue_resend_while_pending"] == {
        "http_status": 200,
        "error_code": None,
        "response_reference": reference,
        "path": "body.data.tracking_code",
        "pending_evidence_path": "issue_pending_probe.json:body.data.status",
    }
    assert result["issue_resend_identity_matches"] is True
    probe = next(r for r in result["records"] if r["name"] == "issue_attempt_1_pending_probe")
    assert probe["body"]["data"]["status"] == "Pending"
    issue_path = "/v1/invoices/issue"
    check_path = f"/v1/invoices/issue/check/{reference}"
    first = fake.paths().index(issue_path)
    assert fake.paths()[first : first + 3] == [issue_path, check_path, issue_path]
    assert result["issue_idempotent"] == "proven"


def test_s7_resend_without_identity_stays_inconclusive(references) -> None:
    fake = FakeSepay(pending_checks=(2,), resend_while_pending="conflict", after_issued="accepted")
    result = run_s7(fake)
    assert result["issue_pending_observed"] is True
    assert result["issue_resend_while_pending"]["http_status"] == 409
    assert result["issue_resend_while_pending"]["error_code"] == "EINVOICE_ISSUE_IN_PROGRESS"
    assert result["issue_resend_while_pending"]["response_reference"] is None
    assert result["issue_resend_identity_matches"] is False
    assert result["issue_idempotent"] == "inconclusive"


def test_s7_issue_after_issued_classifies_same_invoice(references) -> None:
    fake = FakeSepay(after_issued="conflict")
    result = run_s7(fake)
    assert result["issue_after_issued"] == {
        "http_status": 409,
        "error_code": "EINVOICE_DOCUMENT_ISSUED",
        "response_reference": None,
        "path": "body.error.code",
    }
    assert result["issue_after_issued_kind"] == "conflict"
    assert result["issue_after_issued_invoice_number"] == result["invoice_number"]
    assert result["issue_after_issued_same_invoice"] is True
    lookups = [path for path in fake.paths() if path == f"/v1/invoices/{references[0]}"]
    assert len(lookups) == 3  # while draft, after issue, after the issue-again call


def test_s7_flags_new_invoice_number_as_not_idempotent(references) -> None:
    fake = FakeSepay(pending_checks=(2,), after_issued="renumber")
    result = run_s7(fake)
    assert result["issue_pending_observed"] is True
    assert result["issue_after_issued_same_invoice"] is False
    assert result["issue_after_issued_invoice_number"] != result["invoice_number"]
    assert result["issue_idempotent"] == "not_idempotent"


def test_s7_lost_response_recovers_by_reference_only(references, tmp_path: Path) -> None:
    fake = FakeSepay(issue_tracking_is_reference=False)
    result = run_s7(fake, tmp_path)
    lost = references[1]
    assert result["lost_response_reference"] == lost
    assert f"issue-{lost}" in fake.issue_jobs
    assert all(f"issue-{lost}" not in path for path in fake.paths())
    lost_status = json.loads((tmp_path / "lost_issue_status.json").read_text(encoding="utf-8"))
    assert set(lost_status) == {"status", "body_discarded", "classifier_paths"}
    assert lost_status["status"] == 200
    assert result["lost_issue_status"] == 200
    assert result["recover_after_lost_issue_response"] is True
    assert result["recovery_calls"]["lookup"] == 1
    recovered = json.loads((tmp_path / "issue_recovered_lookup.json").read_text(encoding="utf-8"))
    assert recovered["body"]["data"]["reference_code"] == lost
    assert recovered["body"]["data"]["invoice_number"] not in (None, "0")
    # The issue status endpoint does not know the reference as a tracking code here.
    assert result["recovery_by_check"]["check_issue"]["http_status"] == 404
    assert result["recovery_by_check"]["check_issue"]["calls"] == 1
    assert result["recover_by_check_reference_endpoint"] == "check_create"


def test_s7_unknown_reference_records_status_and_code() -> None:
    result = run_s7(FakeSepay())
    assert result["issue_unknown_reference"] == {
        "http_status": 500,
        "error_code": "EINVOICE_DRAFT_NOT_FOUND",
        "state": "rejected",
        "path": "body.error.code",
    }
    # Same status with another code is not mistaken for a missing draft.
    other = run_s7(FakeSepay(unknown_code="CREATE_INVOICE_DATA_FAIL"))
    assert other["issue_unknown_reference"]["http_status"] == 500
    assert other["issue_unknown_reference"]["error_code"] == "CREATE_INVOICE_DATA_FAIL"
    assert other["issue_unknown_reference"]["state"] == "error"


def test_s7_quota_deltas_measured_without_interleaving() -> None:
    fake = FakeSepay(pending_checks=(1,), draft_quota_cost=1, issue_quota_cost=1)
    result = run_s7(fake)
    log = [(method, path) for method, path, _ in fake.requests]
    usage = [index for index, (_, path) in enumerate(log) if path == "/v1/usage"]
    quota_0, quota_1, _, quota_2, quota_3 = usage[:5]
    assert quota_1 == quota_0 + 2
    assert log[quota_0 + 1] == ("POST", "/v1/invoices/create")
    between = log[quota_2 + 1 : quota_3]
    assert {path for _, path in between} == {
        "/v1/invoices/issue",
        f"/v1/invoices/issue/check/{result['reference_code']}",
    }
    assert ("POST", "/v1/invoices/create") not in between
    assert result["quota_delta_draft_create"] == 1
    assert result["draft_consumes_quota"] is True
    assert result["quota_delta_issue"] == 1
    assert result["issue_consumes_quota"] is True
    assert result["quota_window_includes_resend"] is True
    free = run_s7(FakeSepay(draft_quota_cost=0, issue_quota_cost=0))
    assert free["quota_delta_draft_create"] == 0
    assert free["draft_consumes_quota"] is False
    assert free["issue_consumes_quota"] is False


def test_s7_immediate_completion_does_not_prove_pending_idempotency(references) -> None:
    fake = FakeSepay(after_issued="accepted")
    result = run_s7(fake)
    assert result["issue_pending_observed"] is False
    assert result["issue_resend_while_pending"] is None
    assert result["issue_pending_references_tried"] == 3
    attempt_references = [attempt["reference"] for attempt in result["issue_pending_attempts"]]
    assert len(set(attempt_references)) == 3
    issued = [body["reference_code"] for body in fake.issue_bodies()]
    # Each window reference is issued exactly once; only the issue-again step repeats R.
    assert [issued.count(reference) for reference in attempt_references] == [2, 1, 1]
    assert result["issue_after_issued_same_invoice"] is True
    assert result["issue_after_issued_identity_matches"] is True
    assert result["issue_idempotent"] == "inconclusive"
    creates = [path for path in fake.paths() if path == "/v1/invoices/create"]
    assert len(creates) == 5  # R, R again, two window drafts, L


def test_s7_check_by_reference_recovery_recorded_separately(references) -> None:
    lost = references[1]
    fake = FakeSepay(issue_tracking_is_reference=False, lookup_hides_number=frozenset({lost}))
    result = run_s7(fake)
    assert result["recover_after_lost_issue_response"] is False
    assert result["recover_by_check_reference"] is True
    assert result["recover_by_check_reference_endpoint"] == "check_create"
    assert result["recovery_calls"] == {"lookup": 10, "check_issue": 1, "check_create": 1}
    assert result["recovery_by_check"]["check_create"]["identity_matches"] is True
    assert result["classifier_paths"]["recover_by_check_reference"].startswith(
        "issue_recovered_check_create.json:"
    )
    lookups = [path for path in fake.paths() if path == f"/v1/invoices/{lost}"]
    assert len(lookups) == 10
