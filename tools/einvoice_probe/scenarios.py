"""Probe workflows; raw responses are redacted only when persisted."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from .candidates import CANDIDATES
from .client import ProbeClient
from .redact import redact

POLL_INTERVAL_SECONDS = 2.5
POLL_TIMEOUT_SECONDS = 150.0
POLL_LIMIT = 61
INVALID_NONEXISTENT_TAX_CODE = "9999999997"
MAX_PER_PAGE = "100"
AMBIGUOUS_TEMPLATE_MESSAGE = (
    "Multiple invoice templates or series are available; supply "
    "--template-code and --invoice-series."
)


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
    created = _call(client, "create", payload=_invoice_payload(ref, tax_rate, issued_date, account))
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
    payload = _invoice_payload(ref, tax_rate, issued_date, account)
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
            client, "create", payload=_invoice_payload(control_ref, tax_rate, issued_date, account)
        )
    control_accepted = 200 <= control.get("status", 500) < 300
    ref = str(uuid4())
    invalid_payload = _invoice_payload(
        ref, tax_rate, issued_date, account, tax_code=INVALID_NONEXISTENT_TAX_CODE
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
            retry_payload = _invoice_payload(ref, tax_rate, issued_date, account)
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
    tax_code: str | None = None,
) -> dict[str, Any]:
    rate = _as_decimal(tax_rate, "tax_rate")
    buyer: dict[str, str] = {"name": "Probe Buyer", "email": "probe@example.com"}
    if tax_code:
        buyer["tax_code"] = tax_code
    unit_price = Decimal("1000")
    tax_amount = _line_tax_amount(unit_price, rate)
    return {
        "reference_code": reference_code,
        "is_draft": False,
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
                "item_name": "Probe item",
                "unit": "lần",
                "quantity": 1,
                "unit_price": int(unit_price),
                "tax_rate": int(rate),
                "tax_amount": tax_amount,
            }
        ],
        "total_amount": int(unit_price + tax_amount),
    }


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
