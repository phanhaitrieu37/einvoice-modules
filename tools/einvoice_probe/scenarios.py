"""Probe workflows; raw responses are redacted only when persisted."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from . import client as client_module
from .candidates import CANDIDATES
from .client import ProbeClient
from .redact import redact

POLL_INTERVAL_SECONDS = 2.5
POLL_TIMEOUT_SECONDS = 150.0
POLL_LIMIT = 61
INVALID_NONEXISTENT_TAX_CODE = "9999999997"
# Synthetic item code required by SePay for line_type 1|2; sandbox probe data only.
PROBE_ITEM_CODE = "PROBE-ITEM-001"
PROBE_TAX_MATRIX = (
    (-2, "PROBE-KCT", "Probe item KCT"),
    (-1, "PROBE-KKKNT", "Probe item KKKNT"),
    (0, "PROBE-VAT0", "Probe item VAT0"),
    (5, "PROBE-VAT5", "Probe item VAT5"),
    (8, "PROBE-VAT8", "Probe item VAT8"),
    (10, "PROBE-VAT10", "Probe item VAT10"),
)
MAX_PER_PAGE = "100"
AMBIGUOUS_TEMPLATE_MESSAGE = (
    "Multiple invoice templates or series are available; supply "
    "--template-code and --invoice-series."
)

# Draft and issue probes: poll schedule and bounds from the probe contract.
POLL_SCHEDULE_SECONDS = (3, 5, 10, 20, 30, 60)
POLL_SCHEDULE_TIMEOUT_SECONDS = 600.0
RECOVERY_MAX_CALLS = 10
PENDING_REFERENCE_LIMIT = 3
PENDING_STATES = frozenset({"pending", "processing"})
STOP_STATUSES = frozenset({401, 403})
S8_PHASES = ("create", "issue", "backdate")
# Fallback buyer address probe: the two forms of an empty address, in the order they are sent.
S9_VARIANTS = (("variant_omitted", "omitted"), ("variant_empty", "empty"))
BLANK_ADDRESS_STATES = frozenset({"missing", "null", "empty"})
CHECK_ADDRESS_PATH = "body.data.invoice.buyer.address"
LOOKUP_ADDRESS_PATH = "body.data.buyer.address"
SAIGON = ZoneInfo("Asia/Ho_Chi_Minh")
# theo-doi-trang-thai-phat-hanh-hoa-don: the outcome is unknown; SePay says not to resend.
UNCONFIRMED_RESULT_MESSAGE = (
    "Hệ thống chưa xác nhận được kết quả phát hành với nhà cung cấp hóa đơn"
)
JOB_STATE_PATH = "body.data.status"
ERROR_CODE_PATH = "body.error.code"
QUOTA_PATH = "body.data.quota_remaining"
QUOTA_PATHS = {"quota_remaining": QUOTA_PATH}
CREATE_PATHS = {
    "http_status": "status",
    "tracking_code": "body.data.tracking_code",
    "error_code": ERROR_CODE_PATH,
}
ISSUE_PATHS = {
    "http_status": "status",
    "error_code": ERROR_CODE_PATH,
    "tracking_code": "body.data.tracking_code",
    "response_reference": "body.data.reference_code",
}
CHECK_PATHS = {
    "job_state": JOB_STATE_PATH,
    "reference": "body.data.reference_code",
    "invoice_status": "body.data.invoice.status",
    "invoice_number": "body.data.invoice.invoice_number",
    "issued_date": "body.data.invoice.issued_date",
}
LOOKUP_PATHS = {
    "http_status": "status",
    "reference": "body.data.reference_code",
    "invoice_status": "body.data.status",
    "invoice_number": "body.data.invoice_number",
    "issued_date": "body.data.issued_date",
}


def accounts(client: ProbeClient, out_dir: str | Path | None = None) -> dict[str, Any]:
    listing = _call(client, "provider_accounts", params={"page": "1", "per_page": MAX_PER_PAGE})
    records = _account_records(_value(listing.get("body", {}), "data"))
    details = [
        _call(
            client,
            "provider_account_detail",
            path_values={"id": str(_value(record, "id", "provider_account_id"))},
        )
        for record in records
        if _value(record, "id", "provider_account_id")
    ]
    listing["provider_account_details"] = details
    if out_dir is not None:
        write_json(out_dir, "provider_accounts", listing)
        write_json(out_dir, "token_response", client.token_response)
        for index, detail in enumerate(details, start=1):
            write_json(out_dir, f"provider_account_detail_{index:03d}", detail)
    return listing


def select_provider_account(
    response: dict[str, Any],
    client: ProbeClient | None = None,
    *,
    template_code: str | None = None,
    invoice_series: str | None = None,
) -> dict[str, str]:
    envelope = response.get("body", {})
    data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
    records = _account_records(data)
    selected: list[dict[str, str]] = []
    ambiguous = False
    for record in records:
        if record.get("active") is False:
            continue
        detail = _find_provider_details(record, template_code, invoice_series)
        detail["provider_account_id"] = str(
            _value(record, "provider_account_id", "id") or detail["provider_account_id"]
        )
        stored_details = response.get("provider_account_details", [])
        saved = next(
            (
                _data(item)
                for item in stored_details
                if isinstance(item, dict)
                and str(_value(_data(item), "id", "provider_account_id"))
                == detail["provider_account_id"]
            ),
            {},
        )
        nested = _find_provider_details(saved, template_code, invoice_series)
        detail.update({key: value or nested[key] for key, value in detail.items()})
        sources = [record, saved]
        if (
            client
            and detail["provider_account_id"]
            and not (detail["template_code"] and detail["invoice_series"])
        ):
            response_detail = _call(
                client,
                "provider_account_detail",
                path_values={"id": detail["provider_account_id"]},
            )
            nested = _find_provider_details(_data(response_detail), template_code, invoice_series)
            detail.update({key: value or nested[key] for key, value in detail.items()})
            sources.append(_data(response_detail))
        if all(
            detail.get(key) for key in ("provider_account_id", "template_code", "invoice_series")
        ):
            selected.append(detail)
        elif any(
            len(_template_choices(source, template_code, invoice_series)) > 1 for source in sources
        ):
            ambiguous = True
    if not selected and ambiguous:
        raise RuntimeError(AMBIGUOUS_TEMPLATE_MESSAGE)
    if len(selected) != 1:
        hint = " Use --template-code and --invoice-series to select an option." if records else ""
        raise RuntimeError(
            f"expected exactly one usable provider account; found {len(selected)}.{hint}"
        )
    return selected[0]


def s1_create_poll(
    client: ProbeClient,
    *,
    tax_rate: int | str,
    issued_date: str,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
    invoice_series: str | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code, invoice_series=invoice_series
    )
    ref = str(uuid4())
    created = _call(
        client,
        "create",
        payload=_invoice_payload(ref, tax_rate, issued_date, account, is_draft=False),
    )
    tracking = _value(_data(created), "tracking_code")
    polls, exhausted = (
        _poll(client, str(tracking), sleep=sleep, clock=clock)
        if 200 <= created["status"] < 300 and tracking
        else ([], False)
    )
    result: dict[str, Any] = {
        "create": created,
        "tracking_code": str(tracking) if tracking else None,
        "polls": polls,
        "poll_exhausted": exhausted,
        "reference_code": ref,
    }
    result["scenario"] = _success(result)
    return result


def s2_duplicate_reference(
    client: ProbeClient,
    *,
    tax_rate: int | str,
    issued_date: str,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
    invoice_series: str | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code, invoice_series=invoice_series
    )
    ref = str(uuid4())
    payload = _invoice_payload(ref, tax_rate, issued_date, account, is_draft=False)
    first = _call(client, "create", payload=payload)
    tracking = _value(_data(first), "tracking_code")
    polls, exhausted = (
        _poll(client, str(tracking), sleep=sleep, clock=clock)
        if 200 <= first["status"] < 300 and tracking
        else ([], False)
    )
    duplicate = _call(client, "create", payload=payload)
    lookups = []
    for key in ("get_by_reference", "list_invoices"):
        response = _call(
            client,
            key,
            path_values={"reference_code": ref},
            params={"page": "1", "per_page": MAX_PER_PAGE} if key == "list_invoices" else None,
        )
        response["candidate"] = key
        response["matched"] = _lookup_matches(key, response, ref)
        lookups.append(response)
    result = {
        "first": first,
        "first_state": _state(polls[-1]) if polls else None,
        "first_polls": polls,
        "first_poll_exhausted": exhausted,
        "duplicate": duplicate,
        "lookups": lookups,
        "reference_code": ref,
    }
    result["scenario"] = (
        "PASS"
        if 200 <= first["status"] < 300
        and not exhausted
        and bool(polls)
        and 200 <= polls[-1]["status"] < 300
        and _state(polls[-1]) == "Success"
        and duplicate["status"] == 409
        and any(item["matched"] for item in lookups)
        else "FAIL"
    )
    return result


def s3_failed_then_retry(
    client: ProbeClient,
    *,
    tax_rate: int | str,
    issued_date: str,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
    invoice_series: str | None = None,
    control_create: dict[str, Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code, invoice_series=invoice_series
    )
    control = control_create
    if control is None:
        control_ref = str(uuid4())
        control = _call(
            client,
            "create",
            payload=_invoice_payload(control_ref, tax_rate, issued_date, account, is_draft=False),
        )
    control_accepted = 200 <= control.get("status", 500) < 300
    ref = str(uuid4())
    invalid_payload = _invoice_payload(
        ref, tax_rate, issued_date, account, is_draft=False, tax_code=INVALID_NONEXISTENT_TAX_CODE
    )
    created = _call(client, "create", payload=invalid_payload)
    error_code = _error_code(created)
    retry: dict[str, Any] | None = None
    polls: list[dict[str, Any]] = []
    poll_exhausted = False
    reason: str | None = None
    if created["status"] in {400, 422}:
        variant = "sync_reject" if control_accepted else "not_triggerable"
        if not control_accepted:
            reason = "control_create_failed"
    elif created["status"] >= 300:
        variant, reason = "not_triggerable", f"create_http_{created['status']}"
    else:
        tracking = _value(_data(created), "tracking_code")
        if tracking:
            polls, poll_exhausted = _poll(client, str(tracking), sleep=sleep, clock=clock)
        final_state = _state(polls[-1]) if polls else None
        if final_state == "Failed":
            retry_payload = _invoice_payload(ref, tax_rate, issued_date, account, is_draft=False)
            retry_create = _call(client, "create", payload=retry_payload)
            retry_tracking = _value(_data(retry_create), "tracking_code")
            retry_polls, retry_exhausted = (
                _poll(client, str(retry_tracking), sleep=sleep, clock=clock)
                if 200 <= retry_create["status"] < 300 and retry_tracking
                else ([], False)
            )
            retry = {
                "create": retry_create,
                "create_status": retry_create["status"],
                "polls": retry_polls,
                "poll_exhausted": retry_exhausted,
            }
            variant = "async_failed"
        elif poll_exhausted:
            variant, reason = "not_triggerable", "pending_at_bound"
        elif final_state == "Success":
            variant, reason = "not_triggerable", "created_success"
        else:
            variant, reason = "not_triggerable", "failed_create_or_poll_missing_tracking"
    result = {
        "variant": variant,
        "reason": reason,
        "control_create_status": control.get("status"),
        "create": created,
        "create_status": created["status"],
        "error_code": error_code,
        "polls": polls,
        "poll_exhausted": poll_exhausted,
        "retry": retry,
        "retry_create_status": retry.get("create_status") if retry else None,
        "reference_code": ref,
    }
    result["scenario"] = (
        "PASS"
        if variant == "async_failed"
        and retry
        and retry["polls"]
        and 200 <= retry["polls"][-1]["status"] < 300
        and _state(retry["polls"][-1]) == "Success"
        else "INCONCLUSIVE"
    )
    return result


def s4_download(
    client: ProbeClient,
    *,
    tax_rate: int | str,
    issued_date: str,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
    invoice_series: str | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code, invoice_series=invoice_series
    )
    issued = s1_create_poll(
        client,
        tax_rate=tax_rate,
        issued_date=issued_date,
        account=account,
        sleep=sleep,
        clock=clock,
    )
    final = issued.get("polls", [])[-1] if issued.get("polls") else {}
    invoice = _value(_data(final), "invoice") or {}
    urls = {"pdf": _value(invoice, "pdf_url"), "xml": _value(invoice, "xml_url")}
    downloads = {
        kind: _download_with_fallback(
            client, issued.get("tracking_code"), issued.get("reference_code"), kind
        )
        for kind in ("pdf", "xml")
    }
    result = {
        "issue": issued,
        "pdf_url": urls["pdf"],
        "xml_url": urls["xml"],
        "downloads": downloads,
    }
    issue_succeeded = issued["scenario"] == "PASS"
    result["scenario"] = (
        "PASS"
        if issue_succeeded
        and all(
            200 <= item["status"] < 300
            and item["size"] > 0
            and item["file_type"] == kind
            and item["file_name"]
            for kind, item in downloads.items()
        )
        else "FAIL"
    )
    return result


def s6_quota(client: ProbeClient) -> dict[str, Any]:
    result = _call(client, "quota")
    result["quota_endpoint"] = (
        True if 200 <= result["status"] < 300 else False if result["status"] == 404 else None
    )
    result["scenario"] = (
        "ABSENT"
        if result["status"] == 404
        else "PASS"
        if 200 <= result["status"] < 300
        else "INCONCLUSIVE"
    )
    return result


def tax_matrix(
    client: ProbeClient,
    *,
    issued_date: str,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
    invoice_series: str | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code, invoice_series=invoice_series
    )
    results = []
    for rate, item_code, item_name in PROBE_TAX_MATRIX:
        reference = str(uuid4())
        payload = _invoice_payload(
            reference,
            rate,
            issued_date,
            account,
            is_draft=False,
            item_code=item_code,
            item_name=item_name,
        )
        created = _call(client, "create", payload=payload)
        tracking = _value(_data(created), "tracking_code")
        polls, exhausted = (
            _poll(client, str(tracking), sleep=sleep, clock=clock)
            if 200 <= created["status"] < 300 and tracking
            else ([], False)
        )
        final = _data(polls[-1]) if polls else {}
        invoice = _value(final, "invoice") or {}
        results.append(
            {
                "tax_rate": rate,
                "item_code": item_code,
                "item_name": item_name,
                "reference_code": reference,
                "create": created,
                "tracking_code": str(tracking) if tracking else None,
                "polls": polls,
                "poll_exhausted": exhausted,
                "final_status": _state(polls[-1]) if polls else None,
                "tax_amount": _value(invoice, "tax_amount"),
                "total_amount": _value(invoice, "total_amount"),
                "error_code": _error_code(created) if created["status"] >= 300 else None,
                "error_message": _value(_value(created.get("body", {}), "error") or {}, "message")
                if created["status"] >= 300
                else None,
            }
        )
    return {"issued_date": issued_date, "results": results}


def error_401(client: ProbeClient) -> dict[str, Any]:
    status, body, headers = client.request_json(
        "GET", str(CANDIDATES["provider_accounts"]["path"]), bearer_token="fake-probe-token"
    )
    return {"status": status, "body": body, "headers": headers}


def error_422(client: ProbeClient) -> dict[str, Any]:
    status, body, headers = client.request_json(
        "POST", str(CANDIDATES["create"]["path"]), payload={}
    )
    return {"status": status, "body": body, "headers": headers}


def evidence(
    client: ProbeClient,
    *,
    tax_rate: int | str,
    issued_date: str,
    template_code: str | None = None,
    invoice_series: str | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    on_result: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    account: dict[str, str] | None = None

    def run(name: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            result = operation()
        except Exception as error:
            result = {"scenario": "ERROR", "error_class": type(error).__name__}
        results[name] = result
        if on_result:
            on_result(name, result)
        return result

    account_result = run(
        "accounts",
        lambda: accounts(client),
    )
    try:
        account = select_provider_account(
            account_result, client, template_code=template_code, invoice_series=invoice_series
        )
    except Exception as error:
        message = str(error)
        if "--template-code" in message:
            message = AMBIGUOUS_TEMPLATE_MESSAGE
        account_error = {
            "scenario": "ERROR",
            "error_class": type(error).__name__,
            "message": message,
        }
        results["account_selection"] = account_error
        if on_result:
            on_result("account_selection", account_error)

    def invoice_scenario(name: str, operation: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        if account is None:
            result = {"scenario": "ERROR", "error_class": "AccountSelectionError"}
            results[name] = result
            if on_result:
                on_result(name, result)
            return result
        return run(
            name,
            lambda: operation(
                client,
                tax_rate=tax_rate,
                issued_date=issued_date,
                account=account,
                sleep=sleep,
                clock=clock,
            ),
        )

    s1 = invoice_scenario("s1", s1_create_poll)
    invoice_scenario("s2", s2_duplicate_reference)
    control = s1.get("create") if s1.get("scenario") != "ERROR" else None
    if account is None:
        s3 = {"scenario": "ERROR", "error_class": "AccountSelectionError"}
        results["s3"] = s3
        if on_result:
            on_result("s3", s3)
    else:
        s3 = run(
            "s3",
            lambda: s3_failed_then_retry(
                client,
                tax_rate=tax_rate,
                issued_date=issued_date,
                account=account,
                control_create=control,
                sleep=sleep,
                clock=clock,
            ),
        )
    invoice_scenario("s4", s4_download)
    run("s6", lambda: s6_quota(client))
    return {
        "environment": "test",
        "generated_at": datetime.now(UTC).isoformat(),
        "scenarios": {
            key: results.get(key, {}).get("scenario", "ERROR")
            for key in ("s1", "s2", "s3", "s4", "s6")
        },
        "details": {"s3_variant": s3.get("variant"), **results},
    }


class ProbeStopped(RuntimeError):
    """A response that needs a human decision (401/403) ended the scenario early."""


class SameDayError(RuntimeError):
    """The cross-day issue phase ran on or before the calendar day the drafts were created."""


class _Recorder:
    """Records every call in order through ``record_call`` and stops on 401/403."""

    def __init__(self, out_dir: str | Path | None) -> None:
        self.out_dir = out_dir
        self.records: list[dict[str, Any]] = []

    def record(
        self, name: str, response: dict[str, Any], classifier_paths: dict[str, str]
    ) -> dict[str, Any]:
        record = record_call(self.out_dir, name, response, classifier_paths)
        self.records.append({"name": name, **record})
        if response.get("status") in STOP_STATUSES:
            raise ProbeStopped(f"{name} returned HTTP {response['status']}")
        return record

    def alias(self, name: str, record: dict[str, Any], source: str) -> dict[str, Any]:
        """Write a canonical copy of an already recorded call (or a summary) under ``name``."""
        aliased = record_call(self.out_dir, name, record, record.get("classifier_paths", {}))
        self.records.append({"name": name, "alias_of": source})
        return aliased

    def poll(
        self,
        prefix: str,
        fetch: Callable[[], dict[str, Any]],
        done: Callable[[dict[str, Any]], bool],
        classifier_paths: dict[str, str],
        *,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        max_calls: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Poll on the 3, 5, 10, 20, 30, 60 s schedule (then every 60 s) for at most 10 minutes."""
        deadline = clock() + POLL_SCHEDULE_TIMEOUT_SECONDS
        calls: list[dict[str, Any]] = []
        while True:
            if calls:
                if max_calls is not None and len(calls) >= max_calls:
                    return calls, True
                remaining = deadline - clock()
                if remaining <= 0:
                    return calls, True
                step = POLL_SCHEDULE_SECONDS[min(len(calls) - 1, len(POLL_SCHEDULE_SECONDS) - 1)]
                sleep(min(step, remaining))
            name = f"{prefix}_poll_{len(calls) + 1:02d}"
            calls.append(self.record(name, fetch(), classifier_paths))
            if done(calls[-1]):
                return calls, False


def s7_draft_then_issue(
    client: ProbeClient,
    *,
    tax_rate: int | str,
    issued_date: str,
    template_code: str | None = None,
    invoice_series: str | None = None,
    account: dict[str, str] | None = None,
    out_dir: str | Path | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Draft, issue, resend and recovery evidence; every call is recorded raw with field paths."""
    sleep, clock = _pace_functions(sleep, clock)
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code, invoice_series=invoice_series
    )
    recorder = _Recorder(out_dir)
    result: dict[str, Any] = {
        "reference_code": str(uuid4()),
        "lost_response_reference": str(uuid4()),
        "issued_date": issued_date,
        "classifier_paths": {},
    }
    try:
        _s7_steps(
            client,
            recorder,
            result,
            tax_rate,
            issued_date,
            account,
            {"sleep": sleep, "clock": clock},
        )
    except ProbeStopped as stopped:
        result["stopped"] = str(stopped)
    result["steps"] = [record["name"] for record in recorder.records]
    result["unconfirmed_result_messages"] = _unconfirmed_messages(recorder.records)
    result["records"] = recorder.records
    return result


def _s7_steps(
    client: ProbeClient,
    recorder: _Recorder,
    result: dict[str, Any],
    tax_rate: int | str,
    issued_date: str,
    account: dict[str, str],
    pace: dict[str, Any],
) -> None:
    reference = result["reference_code"]
    paths = result["classifier_paths"]

    def quota(name: str) -> int | None:
        return _quota_remaining(recorder.record(name, _call(client, "quota"), QUOTA_PATHS))

    def draft_payload(ref: str) -> dict[str, Any]:
        return _invoice_payload(ref, tax_rate, issued_date, account, is_draft=True)

    # 1. Draft quota: nothing but the create itself sits between the two quota reads.
    quota_0 = quota("quota_0")
    created = recorder.record(
        "draft_create_accepted",
        _call(client, "create", payload=draft_payload(reference)),
        CREATE_PATHS,
    )
    quota_1 = quota("quota_1")
    tracking = _at(created, "body.data.tracking_code")
    result.update(
        draft_create_status=created["status"],
        tracking_code=tracking,
        quota_0=quota_0,
        quota_1=quota_1,
        quota_delta_draft_create=_delta(quota_0, quota_1),
        draft_consumes_quota=_consumes(_delta(quota_0, quota_1)),
    )
    paths["draft_consumes_quota"] = f"quota_0.json|quota_1.json:{QUOTA_PATH}"
    if not (_ok(created) and tracking):
        result["draft_create_accepted"] = False
        return

    # 2. Poll the create job to its end; the draft marker comes from the final check.
    draft_polls, draft_exhausted = recorder.poll(
        "draft_check",
        lambda: _call(client, "check_create", path_values={"tracking_code": str(tracking)}),
        _job_done,
        CHECK_PATHS,
        **pace,
    )
    draft_check = recorder.alias("draft_check", draft_polls[-1], "draft_check_poll_last")
    accepted = _job_state(draft_check) == "success"
    quota_processed = quota("quota_1_after_draft_check")
    result.update(
        draft_check_polls=len(draft_polls),
        draft_check_exhausted=draft_exhausted,
        draft_job_state=_at(draft_check, JOB_STATE_PATH),
        draft_create_accepted=accepted,
        draft_marker=_draft_marker(draft_check, CHECK_PATHS) if accepted else None,
        quota_1_after_draft_check=quota_processed,
        quota_delta_draft_after_processing=_delta(quota_0, quota_processed),
    )
    paths["draft_create_accepted"] = (
        f"draft_create_accepted.json:status|draft_check.json:{JOB_STATE_PATH}"
    )
    if result["draft_marker"]:
        paths["draft_marker"] = "draft_check.json:" + result["draft_marker"]["path"]
    if not accepted:
        return

    # 3. Lookup by reference while the draft exists.
    draft_lookup = recorder.record("draft_lookup", _lookup(client, reference), LOOKUP_PATHS)
    result["draft_marker_lookup"] = _draft_marker(draft_lookup, LOOKUP_PATHS)
    if result["draft_marker_lookup"]:
        paths["draft_marker_lookup"] = "draft_lookup.json:" + result["draft_marker_lookup"]["path"]

    # 4. Create the same reference again while the draft still exists.
    recreate = recorder.record(
        "draft_recreate",
        _call(client, "create", payload=draft_payload(reference)),
        CREATE_PATHS,
    )
    result["draft_recreate_behaviour"] = _status_and_code(recreate)
    paths["draft_recreate_behaviour"] = (
        "draft_recreate.json:" + result["draft_recreate_behaviour"]["path"]
    )

    # 5-6. Issue, probe once at once, resend only on observed pending, poll to the end.
    quota_2 = quota("quota_2")
    first = _issue_attempt(client, recorder, reference, "issue_attempt_1")
    recorder.alias("issue_accepted", first["accepted_record"], "issue_attempt_1_accepted")
    issue_check = None
    if first["tracking"]:
        issue_polls, issue_exhausted = recorder.poll(
            "issue_check",
            lambda: _call(client, "check_issue", path_values={"tracking_code": first["tracking"]}),
            _job_done,
            CHECK_PATHS,
            **pace,
        )
        issue_check = recorder.alias(
            "issue_check_success", issue_polls[-1], "issue_check_poll_last"
        )
        first["numbers"].append(_at(issue_check, CHECK_PATHS["invoice_number"]))
        result.update(issue_check_polls=len(issue_polls), issue_check_exhausted=issue_exhausted)
        result["issue_check_state"] = {
            "path": JOB_STATE_PATH,
            "value": _at(issue_check, JOB_STATE_PATH),
        }
        paths["issue_check_state"] = f"issue_check_success.json:{JOB_STATE_PATH}"
        marker = result["draft_marker"]
        if marker:
            result["draft_marker_differs_when_issued"] = _ok(issue_check) and (
                _at(issue_check, marker["path"]) != marker["value"]
            )
    first["numbers"] += _follow_resend(client, recorder, first, "issue_attempt_1", pace)
    quota_3 = quota("quota_3")
    result.update(
        issue_status=first["accepted"]["http_status"],
        issue_tracking_code=first["tracking"],
        issue_tracking_equals_reference=first["tracking"] == reference,
        quota_2=quota_2,
        quota_3=quota_3,
        quota_delta_issue=_delta(quota_2, quota_3),
        issue_consumes_quota=_consumes(_delta(quota_2, quota_3)),
        quota_window_includes_resend=first["resend"] is not None,
    )
    paths["issue_tracking_equals_reference"] = "issue_accepted.json:body.data.tracking_code"
    paths["issue_consumes_quota"] = f"quota_2.json|quota_3.json:{QUOTA_PATH}"

    # 5 (continued). Fresh draft references for the pending window, at most three in total.
    attempts = [first]
    if _ok_status(first["accepted"]["http_status"]) and first["pending"] is None:
        for number in range(2, PENDING_REFERENCE_LIMIT + 1):
            attempts.append(_pending_retry(client, recorder, number, draft_payload, pace))
            if attempts[-1]["pending"] is not None:
                break
    _record_pending_outcome(recorder, result, attempts)

    # 7. Lookup by reference after the issue ended.
    issued_lookup = recorder.record("issue_lookup_issued", _lookup(client, reference), LOOKUP_PATHS)
    lookup_number = _at(issued_lookup, LOOKUP_PATHS["invoice_number"])
    lookup_date = _at(issued_lookup, LOOKUP_PATHS["issued_date"])
    check_number = _at(issue_check, CHECK_PATHS["invoice_number"]) if issue_check else None
    check_date = _at(issue_check, CHECK_PATHS["issued_date"]) if issue_check else None
    first["numbers"].append(lookup_number)
    date_paths = {}
    if check_date:
        date_paths["check"] = CHECK_PATHS["issued_date"]
    if lookup_date:
        date_paths["lookup"] = LOOKUP_PATHS["issued_date"]
    result.update(
        provider_issued_date_path=date_paths,
        issued_date_check=check_date,
        issued_date_lookup=lookup_date,
        invoice_number=lookup_number,
        issued_lookup_has_numbers=_has_number(lookup_number),
        recover_numbers_by_reference=_has_number(lookup_number) and bool(lookup_date),
        issue_endpoint_verified=_ok_status(first["accepted"]["http_status"])
        and (_has_number(lookup_number) or _has_number(check_number)),
    )
    lookup_numbers = f"{LOOKUP_PATHS['invoice_number']}+{LOOKUP_PATHS['issued_date']}"
    paths["provider_issued_date_path"] = (
        f"issue_check_success.json:{CHECK_PATHS['issued_date']}"
        f"|issue_lookup_issued.json:{LOOKUP_PATHS['issued_date']}"
    )
    paths["issued_lookup_has_numbers"] = (
        f"issue_lookup_issued.json:{LOOKUP_PATHS['invoice_number']}"
    )
    paths["recover_numbers_by_reference"] = f"issue_lookup_issued.json:{lookup_numbers}"
    paths["issue_endpoint_verified"] = (
        f"issue_accepted.json:status|issue_check_success.json:{CHECK_PATHS['invoice_number']}"
        f"|issue_lookup_issued.json:{LOOKUP_PATHS['invoice_number']}"
    )

    # 8. Issue again after the invoice is issued; the lookup must keep the same number.
    after = recorder.record(
        "issue_after_issued",
        _call(client, "publish", payload=_issue_payload(reference)),
        ISSUE_PATHS,
    )
    after_outcome = _issue_outcome(after, reference, first["tracking"])
    after_tracking = _at(after, "body.data.tracking_code") if _ok(after) else None
    if after_tracking:
        after_polls, _ = recorder.poll(
            "issue_after_issued_check",
            lambda: _call(
                client, "check_issue", path_values={"tracking_code": str(after_tracking)}
            ),
            _job_done,
            CHECK_PATHS,
            **pace,
        )
        first["numbers"].append(_at(after_polls[-1], CHECK_PATHS["invoice_number"]))
    after_lookup = recorder.record(
        "issue_after_issued_lookup", _lookup(client, reference), LOOKUP_PATHS
    )
    after_number = _at(after_lookup, LOOKUP_PATHS["invoice_number"])
    first["numbers"].append(after_number)
    result.update(
        issue_after_issued={
            key: after_outcome[key]
            for key in ("http_status", "error_code", "response_reference", "path")
        },
        issue_after_issued_kind=after_outcome["kind"],
        issue_after_issued_identity_matches=after_outcome["identity_matches"],
        issue_after_issued_invoice_number=after_number,
        issue_after_issued_same_invoice=_has_number(after_number) and after_number == lookup_number,
    )
    paths["issue_after_issued"] = "issue_after_issued.json:" + after_outcome["path"]
    paths["issue_after_issued_same_invoice"] = (
        f"issue_lookup_issued.json|issue_after_issued_lookup.json:{LOOKUP_PATHS['invoice_number']}"
    )

    # 9. Lost issue response: recover with the reference only, measuring each path apart.
    _lost_response_recovery(client, recorder, result, draft_payload, pace)

    # 10. Issue a reference that was never created.
    unknown_reference = str(uuid4())
    unknown = recorder.record(
        "issue_unknown_reference",
        _call(client, "publish", payload=_issue_payload(unknown_reference)),
        ISSUE_PATHS,
    )
    unknown_outcome = _issue_outcome(unknown, unknown_reference, None)
    result["issue_unknown_reference"] = {
        "http_status": unknown_outcome["http_status"],
        "error_code": unknown_outcome["error_code"],
        "state": unknown_outcome["kind"],
        "path": unknown_outcome["path"],
    }
    paths["issue_unknown_reference"] = "issue_unknown_reference.json:" + unknown_outcome["path"]
    result["issue_idempotent"] = _issue_idempotency(result, attempts)
    paths["issue_idempotent"] = (
        "issue_pending_probe.json|issue_resend_pending.json|issue_after_issued.json"
        "|issue_lookup_issued.json|issue_after_issued_lookup.json"
    )


def _issue_attempt(
    client: ProbeClient, recorder: _Recorder, reference: str, prefix: str
) -> dict[str, Any]:
    """Issue once, probe once straight away, and resend only when the probe shows pending."""
    accepted = recorder.record(
        f"{prefix}_accepted",
        _call(client, "publish", payload=_issue_payload(reference)),
        ISSUE_PATHS,
    )
    tracking = _at(accepted, "body.data.tracking_code") if _ok(accepted) else None
    attempt = _attempt(reference)
    attempt.update(
        accepted=_issue_outcome(accepted, reference, None),
        accepted_record=accepted,
        tracking=str(tracking) if tracking else None,
    )
    if not _ok(accepted):
        return attempt
    if tracking:
        endpoint = "check_issue"
        response = _call(client, "check_issue", path_values={"tracking_code": str(tracking)})
    else:
        endpoint = "get_by_reference"
        response = _lookup(client, reference)
    probe = recorder.record(
        f"{prefix}_pending_probe",
        response,
        {"pending_state": JOB_STATE_PATH, "endpoint": endpoint},
    )
    attempt.update(probe_record=probe, probe_endpoint=endpoint, pending=_pending_marker(probe))
    if attempt["pending"] is not None:
        resend = recorder.record(
            f"{prefix}_resend",
            _call(client, "publish", payload=_issue_payload(reference)),
            ISSUE_PATHS,
        )
        attempt.update(
            resend_record=resend,
            resend=_issue_outcome(resend, reference, attempt["tracking"]),
        )
    return attempt


def _attempt(reference: str) -> dict[str, Any]:
    return {
        "reference": reference,
        "accepted": {"http_status": None, "kind": "not_sent"},
        "accepted_record": None,
        "tracking": None,
        "probe_record": None,
        "probe_endpoint": None,
        "pending": None,
        "resend": None,
        "resend_record": None,
        "numbers": [],
        "skipped": None,
    }


def _follow_resend(
    client: ProbeClient,
    recorder: _Recorder,
    attempt: dict[str, Any],
    prefix: str,
    pace: dict[str, Any],
) -> list[Any]:
    """Poll an accepted resend that returned a tracking code of its own to its end."""
    resend = attempt["resend_record"]
    tracking = _at(resend, "body.data.tracking_code") if resend and _ok(resend) else None
    if not tracking or tracking == attempt["tracking"]:
        return []
    polls, _ = recorder.poll(
        f"{prefix}_resend_check",
        lambda: _call(client, "check_issue", path_values={"tracking_code": str(tracking)}),
        _job_done,
        CHECK_PATHS,
        **pace,
    )
    return [_at(polls[-1], CHECK_PATHS["invoice_number"])]


def _pending_retry(
    client: ProbeClient,
    recorder: _Recorder,
    number: int,
    draft_payload: Callable[[str], dict[str, Any]],
    pace: dict[str, Any],
) -> dict[str, Any]:
    """One more fresh draft reference for the pending window; the caller bounds the count."""
    prefix = f"issue_attempt_{number}"
    reference = str(uuid4())
    created = recorder.record(
        f"{prefix}_draft_create",
        _call(client, "create", payload=draft_payload(reference)),
        CREATE_PATHS,
    )
    tracking = _at(created, "body.data.tracking_code")
    if not (_ok(created) and tracking):
        return {**_attempt(reference), "skipped": "draft_create_rejected"}
    polls, _ = recorder.poll(
        f"{prefix}_draft_check",
        lambda: _call(client, "check_create", path_values={"tracking_code": str(tracking)}),
        _job_done,
        CHECK_PATHS,
        **pace,
    )
    if _job_state(polls[-1]) != "success":
        return {**_attempt(reference), "skipped": "draft_not_created"}
    attempt = _issue_attempt(client, recorder, reference, prefix)
    if attempt["pending"] is None:
        return attempt
    if attempt["tracking"]:
        issue_polls, _ = recorder.poll(
            f"{prefix}_issue_check",
            lambda: _call(
                client, "check_issue", path_values={"tracking_code": attempt["tracking"]}
            ),
            _job_done,
            CHECK_PATHS,
            **pace,
        )
        attempt["numbers"].append(_at(issue_polls[-1], CHECK_PATHS["invoice_number"]))
    attempt["numbers"] += _follow_resend(client, recorder, attempt, prefix, pace)
    lookup = recorder.record(f"{prefix}_lookup", _lookup(client, reference), LOOKUP_PATHS)
    attempt["numbers"].append(_at(lookup, LOOKUP_PATHS["invoice_number"]))
    return attempt


def _record_pending_outcome(
    recorder: _Recorder, result: dict[str, Any], attempts: list[dict[str, Any]]
) -> None:
    pending = next((attempt for attempt in attempts if attempt["pending"] is not None), None)
    summary = [
        {
            "reference": attempt["reference"],
            "issue_status": attempt["accepted"]["http_status"],
            "probe_endpoint": attempt["probe_endpoint"],
            "probe_state": _at(attempt["probe_record"], JOB_STATE_PATH)
            if attempt["probe_record"]
            else None,
            "pending": attempt["pending"],
            "skipped": attempt["skipped"],
        }
        for attempt in attempts
    ]
    probed = [attempt for attempt in attempts if attempt["probe_record"] is not None]
    shown = pending or (probed[-1] if probed else None)
    if shown is not None:
        index = attempts.index(shown) + 1
        recorder.alias(
            "issue_pending_probe", shown["probe_record"], f"issue_attempt_{index}_pending_probe"
        )
    result.update(
        issue_pending_attempts=summary,
        issue_pending_references_tried=len(attempts),
        issue_pending_observed=pending is not None,
    )
    result["classifier_paths"]["issue_pending_observed"] = (
        f"issue_pending_probe.json:{JOB_STATE_PATH}"
    )
    if pending is None:
        result["issue_resend_while_pending"] = None
        result["classifier_paths"]["issue_resend_while_pending"] = (
            "issue_resend_pending.json:issue_pending_observed"
        )
        recorder.alias(
            "issue_resend_pending",
            {"status": None, "issue_pending_observed": False, "attempts": summary},
            "issue_pending_attempts",
        )
        return
    index = attempts.index(pending) + 1
    recorder.alias(
        "issue_resend_pending", pending["resend_record"], f"issue_attempt_{index}_resend"
    )
    result["issue_resend_reference"] = pending["reference"]
    result["issue_resend_kind"] = pending["resend"]["kind"]
    result["issue_resend_identity_matches"] = pending["resend"]["identity_matches"]
    result["issue_resend_while_pending"] = {
        "http_status": pending["resend"]["http_status"],
        "error_code": pending["resend"]["error_code"],
        "response_reference": pending["resend"]["response_reference"],
        "path": pending["resend"]["path"],
        "pending_evidence_path": "issue_pending_probe.json:" + pending["pending"]["path"],
    }
    result["classifier_paths"]["issue_resend_while_pending"] = (
        "issue_resend_pending.json:" + pending["resend"]["path"]
    )


def _lost_response_recovery(
    client: ProbeClient,
    recorder: _Recorder,
    result: dict[str, Any],
    draft_payload: Callable[[str], dict[str, Any]],
    pace: dict[str, Any],
) -> None:
    lost = result["lost_response_reference"]
    paths = result["classifier_paths"]
    result.update(recover_after_lost_issue_response=False, recover_by_check_reference=False)
    created = recorder.record(
        "lost_draft_create", _call(client, "create", payload=draft_payload(lost)), CREATE_PATHS
    )
    tracking = _at(created, "body.data.tracking_code")
    if not (_ok(created) and tracking):
        result["lost_response_skipped"] = "draft_create_rejected"
        return
    draft_polls, _ = recorder.poll(
        "lost_draft_check",
        lambda: _call(client, "check_create", path_values={"tracking_code": str(tracking)}),
        _job_done,
        CHECK_PATHS,
        **pace,
    )
    if _job_state(draft_polls[-1]) != "success":
        result["lost_response_skipped"] = "draft_not_created"
        return
    # The response is dropped: only the HTTP status is kept, never the body or tracking code.
    lost_status = _call(client, "publish", payload=_issue_payload(lost))["status"]
    recorder.record(
        "lost_issue_status",
        {"status": lost_status, "body_discarded": True},
        {"http_status": "status"},
    )

    def recovered(record: dict[str, Any], record_paths: dict[str, str]) -> bool:
        return (
            _ok(record)
            and _has_number(_at(record, record_paths["invoice_number"]))
            and bool(_at(record, record_paths["issued_date"]))
        )

    lookups, _ = recorder.poll(
        "recovery_lookup",
        lambda: _lookup(client, lost),
        lambda record: recovered(record, LOOKUP_PATHS),
        LOOKUP_PATHS,
        max_calls=RECOVERY_MAX_CALLS,
        **pace,
    )
    recorder.alias("issue_recovered_lookup", lookups[-1], "recovery_lookup_poll_last")
    by_check: dict[str, dict[str, Any]] = {}
    for endpoint, name in (
        ("check_issue", "issue_recovered_check"),
        ("check_create", "issue_recovered_check_create"),
    ):
        checks, _ = recorder.poll(
            f"recovery_{endpoint}",
            lambda endpoint=endpoint: _call(client, endpoint, path_values={"tracking_code": lost}),
            lambda record: recovered(record, CHECK_PATHS) or record["status"] in {400, 404},
            CHECK_PATHS,
            max_calls=RECOVERY_MAX_CALLS,
            **pace,
        )
        recorder.alias(name, checks[-1], f"recovery_{endpoint}_poll_last")
        identity = lost in {
            _at(checks[-1], CHECK_PATHS["reference"]),
            _at(checks[-1], "body.data.invoice.reference_code"),
        }
        by_check[endpoint] = {
            "file": f"{name}.json",
            "calls": len(checks),
            "http_status": checks[-1]["status"],
            "identity_matches": identity,
            "recovered": identity and recovered(checks[-1], CHECK_PATHS),
        }
    check_endpoint = next(
        (endpoint for endpoint, value in by_check.items() if value["recovered"]), None
    )
    result.update(
        lost_issue_status=lost_status,
        recovery_calls={
            "lookup": len(lookups),
            **{endpoint: value["calls"] for endpoint, value in by_check.items()},
        },
        recover_after_lost_issue_response=recovered(lookups[-1], LOOKUP_PATHS),
        recover_by_check_reference=check_endpoint is not None,
        recover_by_check_reference_endpoint=check_endpoint,
        recovery_by_check=by_check,
    )
    paths["recover_after_lost_issue_response"] = (
        f"issue_recovered_lookup.json:{LOOKUP_PATHS['invoice_number']}"
        f"+{LOOKUP_PATHS['issued_date']}"
    )
    check_files = (
        by_check[check_endpoint]["file"]
        if check_endpoint
        else "issue_recovered_check.json|issue_recovered_check_create.json"
    )
    paths["recover_by_check_reference"] = (
        f"{check_files}:{CHECK_PATHS['reference']}+{CHECK_PATHS['invoice_number']}"
        f"+{CHECK_PATHS['issued_date']}"
    )


def s8_draft_cross_day(
    client: ProbeClient,
    *,
    phase: str,
    tax_rate: int | str | None,
    issued_date: str,
    references_file: str | Path | None = None,
    draft_issued_date: str | None = None,
    template_code: str | None = None,
    invoice_series: str | None = None,
    account: dict[str, str] | None = None,
    out_dir: str | Path | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Real drafts created on day D and issued on a later Asia/Saigon day; backdate is apart."""
    if phase not in S8_PHASES:
        raise ValueError(f"phase must be one of {', '.join(S8_PHASES)}")
    pace = dict(zip(("sleep", "clock"), _pace_functions(sleep, clock), strict=True))
    run_at = (now or (lambda: datetime.now(SAIGON)))()
    result: dict[str, Any] = {
        "phase": phase,
        "issued_date": issued_date,
        "run_at": run_at.isoformat(),
        "run_on": run_at.date().isoformat(),
        "classifier_paths": {},
    }
    if phase == "issue":
        stored = json.loads(Path(str(references_file)).read_text(encoding="utf-8"))
        if result["run_on"] <= stored["created_on"]:
            raise SameDayError(
                f"same_day: run_on {result['run_on']} is not after created_on "
                f"{stored['created_on']}"
            )
        result.update(created_on=stored["created_on"], references=stored["references"])
    elif phase == "create" and Path(str(references_file)).exists():
        raise FileExistsError(f"references_file_exists: {references_file}")
    recorder = _Recorder(out_dir)
    try:
        if phase == "issue":
            _s8_issue(client, recorder, result, pace)
        else:
            account = account or select_provider_account(
                accounts(client), client, template_code=template_code, invoice_series=invoice_series
            )
            if phase == "create":
                _s8_create(client, recorder, result, tax_rate, account, references_file, pace)
            else:
                _s8_backdate(client, recorder, result, tax_rate, account, draft_issued_date, pace)
    except ProbeStopped as stopped:
        result["stopped"] = str(stopped)
    result["steps"] = [record["name"] for record in recorder.records]
    result["unconfirmed_result_messages"] = _unconfirmed_messages(recorder.records)
    result["records"] = recorder.records
    return result


def _s8_draft(
    client: ProbeClient,
    recorder: _Recorder,
    prefix: str,
    payload: dict[str, Any],
    pace: dict[str, Any],
) -> dict[str, Any]:
    """Create one draft and poll its create job to the end."""
    created = recorder.record(
        f"{prefix}_create", _call(client, "create", payload=payload), CREATE_PATHS
    )
    tracking = _at(created, "body.data.tracking_code")
    outcome: dict[str, Any] = {
        "reference": payload["reference_code"],
        "create": _status_and_code(created),
        "job_state": None,
        "draft_marker": None,
        "accepted": False,
    }
    if not (_ok(created) and tracking):
        return outcome
    polls, exhausted = recorder.poll(
        f"{prefix}_check",
        lambda: _call(client, "check_create", path_values={"tracking_code": str(tracking)}),
        _job_done,
        CHECK_PATHS,
        **pace,
    )
    outcome.update(
        job_state=_at(polls[-1], JOB_STATE_PATH),
        poll_exhausted=exhausted,
        draft_marker=_draft_marker(polls[-1], CHECK_PATHS),
        accepted=_job_state(polls[-1]) == "success",
    )
    return outcome


def _s8_create(
    client: ProbeClient,
    recorder: _Recorder,
    result: dict[str, Any],
    tax_rate: int | str | None,
    account: dict[str, str],
    references_file: str | Path | None,
    pace: dict[str, Any],
) -> None:
    drafts = [
        _s8_draft(
            client,
            recorder,
            f"s8_draft_{label}",
            _invoice_payload(str(uuid4()), tax_rate, result["issued_date"], account, is_draft=True),
            pace,
        )
        for label in ("a", "b")
    ]
    result["drafts"] = drafts
    result["references_written"] = all(
        draft["accepted"] and draft["draft_marker"] for draft in drafts
    )
    result["classifier_paths"]["drafts"] = (
        f"s8_draft_a_check_poll_*.json|s8_draft_b_check_poll_*.json:{JOB_STATE_PATH}"
        f"+{CHECK_PATHS['invoice_status']}"
    )
    if not result["references_written"]:
        return
    target = Path(str(references_file))
    target.parent.mkdir(parents=True, exist_ok=True)
    stored = {
        "created_on": result["run_on"],
        "created_at": result["run_at"],
        "issued_date": result["issued_date"],
        "references": [draft["reference"] for draft in drafts],
    }
    target.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    result["created_on"] = stored["created_on"]


def _s8_issue(
    client: ProbeClient, recorder: _Recorder, result: dict[str, Any], pace: dict[str, Any]
) -> None:
    reference_a, reference_b = result["references"]
    outcome_a = _s8_issue_one(client, recorder, "s8_issue_a", _issue_payload(reference_a), pace)
    outcome_b = _s8_issue_one(
        client,
        recorder,
        "s8_issue_b",
        {**_issue_payload(reference_b), "issued_date": result["issued_date"]},
        pace,
    )
    date_a = outcome_a["issued_on"]
    if not outcome_a["issued"]:
        on_issue = "rejected" if outcome_a["rejected"] else "inconclusive"
    elif date_a == result["run_on"]:
        on_issue = "issue_day"
    elif date_a == result["created_on"]:
        on_issue = "draft_day"
    else:
        on_issue = "other_day"
    requested_on = result["issued_date"][:10]
    if not outcome_b["issued"]:
        accepts: bool | str = False if outcome_b["rejected"] else "inconclusive"
    else:
        accepts = True if outcome_b["issued_on"] == requested_on else "ignored"
    result.update(
        issue_a=outcome_a,
        issue_b=outcome_b,
        issued_date_on_issue=on_issue,
        issue_accepts_issued_date=accepts,
        issue_accepts_issued_date_distinguishable=requested_on != date_a,
    )
    result["classifier_paths"].update(
        issued_date_on_issue=f"s8_issue_a_lookup.json:{LOOKUP_PATHS['issued_date']}",
        issue_accepts_issued_date=(
            f"s8_issue_b.json:status|s8_issue_b_lookup.json:{LOOKUP_PATHS['issued_date']}"
        ),
    )


def _s8_issue_one(
    client: ProbeClient,
    recorder: _Recorder,
    prefix: str,
    payload: dict[str, Any],
    pace: dict[str, Any],
) -> dict[str, Any]:
    """Issue one draft, poll the issue job, then read the issued date back by reference."""
    reference = payload["reference_code"]
    issued = recorder.record(prefix, _call(client, "publish", payload=payload), ISSUE_PATHS)
    outcome = _issue_outcome(issued, reference, None)
    tracking = _at(issued, "body.data.tracking_code") if _ok(issued) else None
    job_state = None
    if tracking:
        polls, _ = recorder.poll(
            f"{prefix}_check",
            lambda: _call(client, "check_issue", path_values={"tracking_code": str(tracking)}),
            _job_done,
            CHECK_PATHS,
            **pace,
        )
        job_state = _job_state(polls[-1])
    lookup = recorder.record(f"{prefix}_lookup", _lookup(client, reference), LOOKUP_PATHS)
    issued_date = _at(lookup, LOOKUP_PATHS["issued_date"])
    number = _at(lookup, LOOKUP_PATHS["invoice_number"])
    return {
        "reference": reference,
        "request_fields": sorted(payload),
        "issue": outcome,
        "job_state": job_state,
        # A 409, 5xx or unclassified error leaves the outcome open, as in the buyer address probe.
        "rejected": outcome["kind"] == "rejected" or job_state == "failed",
        "issued": _ok(lookup) and _has_number(number) and bool(issued_date),
        "invoice_number": number,
        "issued_on": str(issued_date)[:10] if issued_date else None,
        "lookup_status": _at(lookup, LOOKUP_PATHS["invoice_status"]),
    }


def _s8_backdate(
    client: ProbeClient,
    recorder: _Recorder,
    result: dict[str, Any],
    tax_rate: int | str | None,
    account: dict[str, str],
    draft_issued_date: str | None,
    pace: dict[str, Any],
) -> None:
    if not draft_issued_date:
        raise ValueError("draft_issued_date is required for the backdate phase")
    draft = _s8_draft(
        client,
        recorder,
        "s8_backdate_draft",
        _invoice_payload(str(uuid4()), tax_rate, draft_issued_date, account, is_draft=True),
        pace,
    )
    result.update(
        draft_issued_date=draft_issued_date,
        backdate_draft=draft,
        backdate_draft_accepted=bool(draft["accepted"] and draft["draft_marker"]),
    )
    result["classifier_paths"]["backdate_draft_accepted"] = (
        f"s8_backdate_draft_create.json:status|s8_backdate_draft_check_poll_*.json:{JOB_STATE_PATH}"
    )
    if not result["backdate_draft_accepted"]:
        return
    issued = _s8_issue_one(
        client, recorder, "s8_backdate_issue", _issue_payload(draft["reference"]), pace
    )
    on = issued["issued_on"]
    if not issued["issued"]:
        on_issue = "rejected" if issued["rejected"] else "inconclusive"
    elif on == draft_issued_date[:10]:
        on_issue = "draft_day"
    elif on == result["run_on"]:
        on_issue = "issue_day"
    else:
        on_issue = "other_day"
    result.update(backdate_issue=issued, backdate_issued_date_on_issue=on_issue)
    result["classifier_paths"]["backdate_issued_date_on_issue"] = (
        f"s8_backdate_issue_lookup.json:{LOOKUP_PATHS['issued_date']}"
    )


def s9_fallback_buyer_address(
    client: ProbeClient,
    *,
    tax_rate: int | str,
    issued_date: str,
    buyer_name: str,
    template_code: str | None = None,
    invoice_series: str | None = None,
    account: dict[str, str] | None = None,
    out_dir: str | Path | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Issue one invoice per empty-address form and read the address back after issue.

    A complete run sends exactly two creates. Any outcome that is not decisive stops the run
    with no retry and no further create, so a partial run never yields a usable rule.
    """
    pace = dict(zip(("sleep", "clock"), _pace_functions(sleep, clock), strict=True))
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code, invoice_series=invoice_series
    )
    recorder = _Recorder(out_dir)
    result: dict[str, Any] = {
        "issued_date": issued_date,
        "create_count": 0,
        "details": {},
        "classifier_paths": {
            "outcome": f"buyer_address_*_create.json:status+{ERROR_CODE_PATH}"
            f"|buyer_address_*_check.json:{JOB_STATE_PATH}",
            "invoice_number": f"buyer_address_*_check.json:{CHECK_PATHS['invoice_number']}",
            "check_address": f"buyer_address_*_check.json:{CHECK_ADDRESS_PATH}",
            "lookup_address": f"buyer_address_*_lookup.json:{LOOKUP_ADDRESS_PATH}",
        },
    }
    for key, _ in S9_VARIANTS:
        result[key] = _s9_unobserved("inconclusive")
    for key, form in S9_VARIANTS:
        buyer: dict[str, str] = {"name": buyer_name}
        if form == "empty":
            buyer["address"] = ""
        payload = _invoice_payload(str(uuid4()), tax_rate, issued_date, account, is_draft=False)
        payload["buyer"] = buyer
        result["create_count"] += 1
        prefix = f"buyer_address_{form}"
        variant, detail, stop = _s9_variant(client, recorder, prefix, payload, pace)
        result[key] = variant
        result["details"][key] = detail
        if stop:
            result["stopped"] = detail.get("stop_reason")
            break
    result["run_complete"] = all(
        result[key]["outcome"] in {"issued", "rejected"} for key, _ in S9_VARIANTS
    )
    result["address_rule"] = _address_rule(
        result["variant_omitted"], result["variant_empty"], result["run_complete"]
    )
    result["steps"] = [record["name"] for record in recorder.records]
    result["records"] = recorder.records
    return result


def _s9_unobserved(outcome: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "invoice_number_valid": False,
        "check_address": {"path": CHECK_ADDRESS_PATH, "state": "unobserved"},
        "lookup_address": {"path": LOOKUP_ADDRESS_PATH, "state": "unobserved"},
        "error": None,
    }


def _s9_variant(
    client: ProbeClient,
    recorder: _Recorder,
    prefix: str,
    payload: dict[str, Any],
    pace: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """One create, its final check and a lookup; returns (variant, detail, stop the run)."""
    reference = payload["reference_code"]
    variant = _s9_unobserved("inconclusive")
    detail: dict[str, Any] = {"reference": reference, "buyer_fields": sorted(payload["buyer"])}
    check_paths = {**CHECK_PATHS, "address": CHECK_ADDRESS_PATH}
    lookup_paths = {**LOOKUP_PATHS, "address": LOOKUP_ADDRESS_PATH}
    try:
        created = recorder.record(
            f"{prefix}_create", _call(client, "create", payload=payload), CREATE_PATHS
        )
        status, code = created["status"], _error_code(created)
        detail["create"] = {"http_status": status, "error_code": code}
        tracking = _at(created, "body.data.tracking_code")
        if 400 <= status < 500 and status != 429 and code:
            variant.update(outcome="rejected", error={"http_status": status, "error_code": code})
        elif not (_ok(created) and tracking):
            variant["error"] = {"http_status": status, "error_code": code}
            detail["stop_reason"] = f"create returned HTTP {status} without a usable tracking code"
            return variant, detail, True
        else:
            polls, exhausted = recorder.poll(
                f"{prefix}_check",
                lambda: _call(client, "check_create", path_values={"tracking_code": str(tracking)}),
                _job_done,
                check_paths,
                **pace,
            )
            final = recorder.alias(
                f"{prefix}_check", polls[-1], f"{prefix}_check_poll_{len(polls):02d}"
            )
            job_state = _job_state(final)
            detail.update(check_calls=len(polls), poll_exhausted=exhausted, job_state=job_state)
            variant["check_address"] = {
                "path": CHECK_ADDRESS_PATH,
                "state": _address_state(final, CHECK_ADDRESS_PATH),
            }
            if exhausted or not _ok(final):
                variant["error"] = {
                    "http_status": final["status"],
                    "error_code": _error_code(final),
                }
                detail["stop_reason"] = "create job did not reach a final state"
                return variant, detail, True
            if job_state == "failed":
                variant.update(
                    outcome="rejected",
                    error={"http_status": final["status"], "error_code": _error_code(final)},
                )
            elif job_state == "success":
                number = _at(final, CHECK_PATHS["invoice_number"])
                detail["check_invoice_number_present"] = _has_number(number)
                variant["invoice_number_valid"] = _has_number(number)
        lookup = recorder.record(f"{prefix}_lookup", _lookup(client, reference), lookup_paths)
        detail["lookup"] = {"http_status": lookup["status"], "error_code": _error_code(lookup)}
        variant["lookup_address"] = {
            "path": LOOKUP_ADDRESS_PATH,
            "state": _address_state(lookup, LOOKUP_ADDRESS_PATH),
        }
        if variant["outcome"] == "rejected":
            if lookup["status"] >= 500:
                detail["stop_reason"] = f"lookup returned HTTP {lookup['status']} after a rejection"
                return variant, detail, True
            return variant, detail, False
        if variant["invoice_number_valid"] and _ok(lookup):
            variant["outcome"] = "issued"
            return variant, detail, False
        detail["stop_reason"] = "issued invoice could not be confirmed by check and lookup"
        return variant, detail, True
    except ProbeStopped as stopped:
        detail["stop_reason"] = str(stopped)
    except (httpx.HTTPError, RuntimeError) as failure:
        detail["stop_reason"] = f"{type(failure).__name__}: {failure}"
    # A rejection already observed stays decisive; anything else is left inconclusive.
    return variant, detail, True


def _address_state(record: Any, path: str) -> str:
    """missing / null / empty / nonempty for the address key, or unobserved without a buyer."""
    if not _ok(record):
        return "unobserved"
    *parents, key = path.split(".")
    parent = record
    for name in parents:
        parent = _value(parent, name)
    if not isinstance(parent, dict):
        return "unobserved"
    present = [value for name, value in parent.items() if str(name).lower() == key.lower()]
    if not present:
        return "missing"
    value = present[0]
    if value is None:
        return "null"
    return "empty" if value == "" else "nonempty"


def _meets_blank_address_rule(variant: dict[str, Any]) -> bool:
    """Issued with a valid number and an address that stays blank in both check and lookup."""
    return (
        variant["outcome"] == "issued"
        and variant["invoice_number_valid"] is True
        and variant["check_address"]["state"] in BLANK_ADDRESS_STATES
        and variant["lookup_address"]["state"] in BLANK_ADDRESS_STATES
    )


def _address_rule(omitted: dict[str, Any], empty: dict[str, Any], run_complete: bool) -> str:
    if not run_complete:
        return "inconclusive"
    if _meets_blank_address_rule(omitted):
        return "omit"
    if _meets_blank_address_rule(empty):
        return "empty_string"
    return "neither"


def _pace_functions(
    sleep: Callable[[float], None] | None, clock: Callable[[], float] | None
) -> tuple[Callable[[float], None], Callable[[], float]]:
    """Default to the client module's pacing hooks, looked up at call time."""
    return (
        sleep or (lambda seconds: client_module._sleep(seconds)),
        clock or (lambda: client_module._monotonic()),
    )


def _lookup(client: ProbeClient, reference: str) -> dict[str, Any]:
    return _call(client, "get_by_reference", path_values={"reference_code": reference})


def _ok(record: Any) -> bool:
    return isinstance(record, dict) and _ok_status(record.get("status"))


def _ok_status(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def _job_state(record: Any) -> str | None:
    state = _at(record, JOB_STATE_PATH)
    return state.lower() if isinstance(state, str) else None


def _job_done(record: dict[str, Any]) -> bool:
    return not _ok(record) or _job_state(record) in {"success", "failed"}


def _has_number(value: Any) -> bool:
    """SePay shows a draft's invoice number as "0" (theo-doi-trang-thai-xuat-hoa-don)."""
    return value not in (None, "", "0", 0)


def _pending_marker(record: dict[str, Any]) -> dict[str, Any] | None:
    state = _at(record, JOB_STATE_PATH)
    if _ok(record) and isinstance(state, str) and state.lower() in PENDING_STATES:
        return {"path": JOB_STATE_PATH, "value": state}
    return None


def _draft_marker(record: dict[str, Any], record_paths: dict[str, str]) -> dict[str, Any] | None:
    """The first field that tells a draft apart from an issued invoice, with its path."""
    if not _ok(record):
        return None
    status = _at(record, record_paths["invoice_status"])
    if isinstance(status, str) and status and status.lower() != "issued":
        return {"path": record_paths["invoice_status"], "value": status}
    number = _at(record, record_paths["invoice_number"])
    if status is None and not _has_number(number):
        return {"path": record_paths["invoice_number"], "value": number}
    return None


def _status_and_code(record: dict[str, Any]) -> dict[str, Any]:
    code = _error_code(record)
    return {
        "http_status": record["status"],
        "error_code": code,
        "path": ERROR_CODE_PATH if code else "status",
    }


def _issue_outcome(record: dict[str, Any], reference: str, tracking: str | None) -> dict[str, Any]:
    status = record.get("status")
    code = _error_code(record)
    response_reference = reference_path = None
    for path in ("body.data.reference_code", "body.data.tracking_code"):
        value = _at(record, path)
        if isinstance(value, str) and value:
            response_reference, reference_path = value, path
            break
    if _ok_status(status):
        kind = "accepted"
    elif status == 409:
        kind = "conflict"
    elif status in {400, 404, 422} or code == "EINVOICE_DRAFT_NOT_FOUND":
        kind = "rejected"
    else:
        kind = "error"
    identities = {reference, tracking} - {None}
    return {
        "http_status": status,
        "error_code": code,
        "response_reference": response_reference,
        "path": ERROR_CODE_PATH if code else reference_path or "status",
        "kind": kind,
        "identity_matches": response_reference in identities,
    }


def _issue_idempotency(result: dict[str, Any], attempts: list[dict[str, Any]]) -> str:
    """Proven only with observed pending, same-identity answers and one unchanged number."""
    for attempt in attempts:
        if len({number for number in attempt["numbers"] if _has_number(number)}) > 1:
            return "not_idempotent"
    pending = next((attempt for attempt in attempts if attempt["pending"] is not None), None)
    if pending is None or pending["resend"] is None:
        return "inconclusive"
    answers = (
        pending["resend"],
        {
            "kind": result.get("issue_after_issued_kind"),
            "identity_matches": result.get("issue_after_issued_identity_matches"),
        },
    )
    same_identity = all(
        answer["kind"] in {"accepted", "conflict"} and answer["identity_matches"] is True
        for answer in answers
    )
    pending_numbers = {number for number in pending["numbers"] if _has_number(number)}
    if (
        same_identity
        and len(pending_numbers) == 1
        and result.get("issue_after_issued_same_invoice")
    ):
        return "proven"
    return "inconclusive"


def _quota_remaining(record: dict[str, Any]) -> int | None:
    value = _at(record, QUOTA_PATH)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _delta(before: int | None, after: int | None) -> int | None:
    return before - after if before is not None and after is not None else None


def _consumes(delta: int | None) -> bool | str:
    return "unknown" if delta is None else delta > 0


def _unconfirmed_messages(records: list[dict[str, Any]]) -> list[str]:
    """Names of calls whose body carries the "result unconfirmed with the provider" message."""
    needle = unicodedata.normalize("NFC", UNCONFIRMED_RESULT_MESSAGE).casefold()
    return [
        record["name"]
        for record in records
        if needle
        in unicodedata.normalize(
            "NFC", json.dumps(record.get("body"), ensure_ascii=False)
        ).casefold()
    ]


def write_json(out_dir: str | Path, name: str, value: Any) -> Path:
    path = Path(out_dir) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact(value), indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return path


def _call(
    client: ProbeClient,
    name: str,
    *,
    payload: dict[str, Any] | None = None,
    path_values: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    candidate = CANDIDATES[name]
    path = str(candidate["path"])
    for key, value in (path_values or {}).items():
        path = path.replace("{" + key + "}", value)
    status, body, headers = client.request_json(
        str(candidate["method"]), path, payload=payload, params=params
    )
    return {"status": status, "body": body, "headers": headers}


def _invoice_payload(
    reference_code: str,
    tax_rate: str | int,
    issued_on: str,
    account: dict[str, str],
    *,
    is_draft: bool,
    tax_code: str | None = None,
    item_code: str = PROBE_ITEM_CODE,
    item_name: str = "Probe item",
) -> dict[str, Any]:
    rate = _as_decimal(tax_rate, "tax_rate")
    buyer: dict[str, str] = {"name": "Probe Buyer", "email": "probe@example.com"}
    if tax_code:
        buyer["tax_code"] = tax_code
    unit_price = Decimal("1000")
    tax_amount = _line_tax_amount(unit_price, rate)
    return {
        "reference_code": reference_code,
        "is_draft": is_draft,
        "provider_account_id": account["provider_account_id"],
        "template_code": account["template_code"],
        "invoice_series": account["invoice_series"],
        "issued_date": issued_on,
        "currency": "VND",
        "buyer": buyer,
        "items": [
            {
                "line_number": 1,
                "line_type": 1,
                "item_code": item_code,
                "item_name": item_name,
                "unit": "lần",
                "quantity": 1,
                "unit_price": int(unit_price),
                "tax_rate": int(rate),
                "tax_amount": tax_amount,
            }
        ],
        "total_amount": int(unit_price + tax_amount),
    }


def _issue_payload(reference_code: str) -> dict[str, str]:
    """Body of POST /v1/invoices/issue (phat-hanh-hoa-don-dien-tu): the draft reference only."""
    return {"reference_code": reference_code}


def record_call(
    out_dir: str | Path | None,
    name: str,
    response: dict[str, Any],
    classifier_paths: dict[str, str],
) -> dict[str, Any]:
    """Attach the field paths used for classification and persist the redacted raw call."""
    record = {**response, "classifier_paths": dict(classifier_paths)}
    if out_dir is not None:
        write_json(out_dir, name, record)
    return record


def _at(record: Any, path: str) -> Any:
    """Resolve a dotted field path such as ``body.data.invoice.status`` in a recorded call."""
    value = record
    for key in path.split("."):
        value = _value(value, key)
    return value


def _line_tax_amount(
    unit_price: Decimal, tax_rate: Decimal, tax_amount: Decimal | str | int | float | None = None
) -> int:
    if not unit_price.is_finite() or not tax_rate.is_finite():
        raise ValueError("price and tax rate must be finite")
    if tax_amount is not None:
        amount = _as_decimal(tax_amount, "tax_amount")
        if amount < 0:
            raise ValueError("tax amount must not be negative")
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if tax_rate in {Decimal("-2"), Decimal("-1"), Decimal("0")}:
        return 0
    if tax_rate < 0:
        raise ValueError("tax rate must be -2, -1, 0, or positive")
    return int(
        (unit_price * tax_rate / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _as_decimal(value: str | Decimal | int | float | None, field: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{field} must not be a float")
    if value is None:
        raise ValueError(f"{field} is required")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    if field == "tax_rate" and result != result.to_integral_value():
        raise ValueError("tax_rate must be an integer")
    if field == "tax_rate" and result not in {
        Decimal("-2"),
        Decimal("-1"),
        Decimal("0"),
        Decimal("5"),
        Decimal("8"),
        Decimal("10"),
    }:
        raise ValueError("tax_rate must be one of -2, -1, 0, 5, 8, or 10")
    return result


def _poll(
    client: ProbeClient,
    tracking_code: str,
    *,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + POLL_TIMEOUT_SECONDS
    states: list[dict[str, Any]] = []
    while True:
        if states:
            remaining = deadline - clock()
            if remaining <= 0:
                return states, True
            sleep(min(POLL_INTERVAL_SECONDS, remaining))
        result = _call(client, "check_create", path_values={"tracking_code": tracking_code})
        result["retry_next_at"] = _retry_next_at(result)
        states.append(result)
        if result["status"] >= 400:
            return states, False
        if _state(result) in {"Success", "Failed"}:
            return states, False
        if clock() >= deadline or len(states) >= POLL_LIMIT:
            return states, True


def _retry_next_at(result: dict[str, Any]) -> Any:
    body = result.get("body", {})
    data = body.get("data", {}) if isinstance(body, dict) else {}
    retry = _value(data, "retry") or _value(body, "retry")
    return _value(retry, "next_retry_at") if isinstance(retry, dict) else None


def _download_with_fallback(
    client: ProbeClient, tracking_code: str | None, reference_code: str | None, kind: str
) -> dict[str, Any]:
    """Try the tai-hoa-don identifier, then the chi-tiet-hoa-don one once on 404."""
    if not tracking_code:
        return {
            "status": None,
            "size": 0,
            "sha256": None,
            "file_type": None,
            "file_name": None,
            "content_type": None,
            "identifier": None,
            "attempts": [],
            "skipped": "missing_tracking_code",
        }
    result = _download_artifact(client, tracking_code, kind)
    identifier = "tracking_code"
    attempts = [{"identifier": identifier, "status": result["status"]}]
    if result["status"] == 404 and reference_code:
        result = _download_artifact(client, reference_code, kind)
        identifier = "reference_code"
        attempts.append({"identifier": identifier, "status": result["status"]})
    return {**result, "identifier": identifier, "attempts": attempts}


def _download_artifact(client: ProbeClient, identifier: str, kind: str) -> dict[str, Any]:
    path = str(CANDIDATES["download"]["path"]).replace("{tracking_code}", identifier)
    status, response, headers = client.request_json("GET", path, params={"type": kind})
    data = _data({"body": response})
    encoded = _value(data, "content")
    decoded = b""
    if isinstance(encoded, str):
        try:
            # Only MIME line breaks are tolerated; any other non-base64 character rejects
            # the content, so a URL or raw XML cannot decode to a fake artifact.
            decoded = base64.b64decode(encoded.replace("\r", "").replace("\n", ""), validate=True)
        except (ValueError, TypeError):
            decoded = b""
    return {
        "status": status,
        "size": len(decoded),
        "sha256": hashlib.sha256(decoded).hexdigest() if decoded else None,
        "file_type": _value(data, "file_type"),
        "file_name": _value(data, "file_name"),
        "content_type": headers.get("content-type"),
    }


def _find_provider_details(
    record: dict[str, Any],
    preferred_template: str | None = None,
    preferred_series: str | None = None,
) -> dict[str, str]:
    provider_id = _value(record, "provider_account_id", "id")
    template = _value(record, "template_code")
    series = _value(record, "invoice_series")
    details = record.get("details", record.get("templates", []))
    if isinstance(details, dict):
        template = template or _value(details, "template_code")
        series = series or _value(details, "invoice_series")
    if isinstance(details, list):
        choices = _template_choices(record, preferred_template, preferred_series)
        templates = {_value(item, "template_code") for item in choices}
        series_values = {_value(item, "invoice_series") for item in choices}
        if len(templates) == 1:
            template = template or next(iter(templates))
        if len(series_values) == 1:
            series = series or next(iter(series_values))
    return {
        "provider_account_id": str(provider_id or ""),
        "template_code": str(template or ""),
        "invoice_series": str(series or ""),
    }


def _template_choices(
    record: dict[str, Any],
    preferred_template: str | None = None,
    preferred_series: str | None = None,
) -> list[dict[str, Any]]:
    details = record.get("details", record.get("templates", []))
    if not isinstance(details, list):
        return []
    choices = [item for item in details if isinstance(item, dict)]
    if preferred_template:
        choices = [item for item in choices if _value(item, "template_code") == preferred_template]
    if preferred_series:
        choices = [item for item in choices if _value(item, "invoice_series") == preferred_series]
    return choices


def _account_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        records = [
            item
            for item in value
            if isinstance(item, dict) and _value(item, "provider_account_id", "id")
        ]
        return records or [record for item in value for record in _account_records(item)]
    if isinstance(value, dict):
        return [record for nested in value.values() for record in _account_records(nested)]
    return []


def _data(result: dict[str, Any]) -> dict[str, Any]:
    body = result.get("body", {})
    data = body.get("data", {}) if isinstance(body, dict) else {}
    return data if isinstance(data, dict) else {}


def _state(result: dict[str, Any]) -> str | None:
    state = _value(_data(result), "status", "state")
    return state.title() if isinstance(state, str) else None


def _success(result: dict[str, Any]) -> str:
    polls = result.get("polls", [])
    if result.get("create", {}).get("status", 500) >= 300:
        return "FAIL"
    if not polls:
        return "INCONCLUSIVE"
    if 200 <= polls[-1]["status"] < 300 and _state(polls[-1]) == "Success":
        return "PASS"
    if 200 <= polls[-1]["status"] < 300 and _state(polls[-1]) == "Failed":
        return "FAIL"
    return "INCONCLUSIVE"


def _error_code(result: dict[str, Any]) -> str | None:
    body = result.get("body", {})
    error = _value(body, "error")
    return _value(error, "code") if isinstance(error, dict) else None


def _lookup_matches(candidate: str, result: dict[str, Any], reference_code: str) -> bool:
    if not 200 <= result["status"] < 300:
        return False
    data = _data(result)
    if candidate == "get_by_reference":
        return _value(data, "reference_code") == reference_code
    items = _value(data, "items")
    return isinstance(items, list) and any(
        isinstance(item, dict) and _value(item, "reference_code") == reference_code
        for item in items
    )


def _value(obj: Any, *keys: str) -> Any:
    if isinstance(obj, dict):
        normalized = {str(key).lower(): value for key, value in obj.items()}
        for key in keys:
            if key.lower() in normalized:
                return normalized[key.lower()]
    return None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
