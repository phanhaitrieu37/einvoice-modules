"""Probe workflows; raw responses are redacted only when persisted."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from .candidates import CANDIDATES
from .client import ProbeClient
from .redact import redact

POLL_INTERVAL_SECONDS = 0.5
POLL_LIMIT = 10
INVALID_NONEXISTENT_TAX_CODE = "9999999997"
RETRY_NONEXISTENT_TAX_CODE = "8888888880"


def accounts(client: ProbeClient) -> dict[str, Any]:
    return _call(client, "provider_accounts")


def select_provider_account(
    response: dict[str, Any], client: ProbeClient | None = None, *, template_code: str | None = None
) -> dict[str, str]:
    envelope = response.get("body", {})
    data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
    records = _account_records(data)
    selected: list[dict[str, str]] = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict) or record.get("active") is False:
            continue
        detail = _find_provider_details(record, template_code)
        detail["provider_account_id"] = str(
            _value(record, "provider_account_id", "id") or detail["provider_account_id"]
        )
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
            data_detail = _data(response_detail)
            nested = _find_provider_details(data_detail, template_code)
            detail.update({key: value or nested[key] for key, value in detail.items()})
        if all(
            detail.get(key) for key in ("provider_account_id", "template_code", "invoice_series")
        ):
            selected.append(detail)
    if len(selected) != 1:
        raise RuntimeError(f"expected exactly one usable provider account; found {len(selected)}")
    return selected[0]


def s1_create_poll(
    client: ProbeClient,
    *,
    tax_rate: str,
    issued_date: date,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code
    )
    ref = str(uuid4())
    payload = _invoice_payload(ref, tax_rate, issued_date, account)
    created = _call(client, "create", payload=payload)
    result: dict[str, Any] = {"create": created, "polls": []}
    tracking = _value(_data(created), "tracking_code")
    if created["status"] < 400 and tracking:
        result["tracking_code"] = str(tracking)
        result["polls"] = _poll(client, str(tracking))
    result["reference_code"] = ref
    return result


def s2_duplicate_reference(
    client: ProbeClient,
    *,
    tax_rate: str,
    issued_date: date,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code
    )
    ref = str(uuid4())
    payload = _invoice_payload(ref, tax_rate, issued_date, account)
    first = _call(client, "create", payload=payload)
    duplicate = _call(client, "create", payload=payload)
    lookup = _call(client, "get_by_reference", path_values={"reference_code": ref})
    return {"first": first, "duplicate": duplicate, "lookup": lookup, "reference_code": ref}


def s3_failed_then_retry(
    client: ProbeClient,
    *,
    tax_rate: str,
    issued_date: date,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code
    )
    ref = str(uuid4())
    payload = _invoice_payload(
        ref, tax_rate, issued_date, account, tax_code=INVALID_NONEXISTENT_TAX_CODE
    )
    created = _call(client, "create", payload=payload)
    status = created["status"]
    if status in {400, 422}:
        variant, polls, retry = "sync_reject", [], None
    elif status >= 400:
        variant, polls, retry = "not_triggerable", [], None
    else:
        tracking = _value(_data(created), "tracking_code")
        polls = _poll(client, str(tracking)) if tracking else []
        final_state = _state(polls[-1]) if polls else None
        if final_state == "Failed":
            variant = "async_failed"
            retry_payload = _invoice_payload(
                ref, tax_rate, issued_date, account, tax_code=RETRY_NONEXISTENT_TAX_CODE
            )
            retry_create = _call(client, "create", payload=retry_payload)
            retry_tracking = _value(_data(retry_create), "tracking_code")
            retry_polls = _poll(client, str(retry_tracking)) if retry_tracking else []
            retry = {"create": retry_create, "polls": retry_polls}
        else:
            variant, retry = "not_triggerable", None
    return {
        "variant": variant,
        "create": created,
        "polls": polls,
        "retry": retry,
        "reference_code": ref,
    }


def s4_download(
    client: ProbeClient,
    *,
    tax_rate: str,
    issued_date: date,
    account: dict[str, str] | None = None,
    template_code: str | None = None,
) -> dict[str, Any]:
    account = account or select_provider_account(
        accounts(client), client, template_code=template_code
    )
    issued = s1_create_poll(client, tax_rate=tax_rate, issued_date=issued_date, account=account)
    tracking = issued.get("tracking_code")
    poll = issued.get("polls", [])
    final_state = _state(poll[-1]) if poll else None
    download = (
        _call(client, "download", params={"tracking_code": str(tracking), "format": "pdf"})
        if tracking and final_state == "Success"
        else {"status": 0, "body": b"", "headers": {}}
    )
    raw = download.get("body", b"")
    if isinstance(raw, str):
        try:
            raw = base64.b64decode(raw)
        except ValueError:
            raw = raw.encode()
    download["sha256"] = hashlib.sha256(raw).hexdigest() if isinstance(raw, bytes) else None
    download["size"] = len(raw) if isinstance(raw, bytes) else 0
    return {"issue": issued, "download": download}


def s6_quota(client: ProbeClient) -> dict[str, Any]:
    result = _call(client, "quota")
    result["quota_endpoint"] = result["status"] != 404
    result["scenario"] = (
        "ABSENT"
        if result["status"] == 404
        else ("PASS" if result["status"] < 400 else "INCONCLUSIVE")
    )
    return result


def evidence(
    client: ProbeClient, *, tax_rate: str, issued_date: date, template_code: str | None = None
) -> dict[str, Any]:
    selected_account = select_provider_account(
        accounts(client), client, template_code=template_code
    )
    results = {
        "s1": s1_create_poll(
            client, tax_rate=tax_rate, issued_date=issued_date, account=selected_account
        ),
        "s2": s2_duplicate_reference(
            client, tax_rate=tax_rate, issued_date=issued_date, account=selected_account
        ),
        "s3": s3_failed_then_retry(
            client, tax_rate=tax_rate, issued_date=issued_date, account=selected_account
        ),
        "s4": s4_download(
            client, tax_rate=tax_rate, issued_date=issued_date, account=selected_account
        ),
        "s6": s6_quota(client),
    }
    s3_variant = results["s3"]["variant"]
    statuses = {
        "s1": _success(results["s1"]),
        "s2": "PASS"
        if results["s2"]["first"]["status"] < 400
        and results["s2"]["duplicate"]["status"] == 409
        and results["s2"]["lookup"]["status"] < 400
        else "FAIL",
        "s3": "PASS"
        if s3_variant == "async_failed"
        and _state(results["s3"].get("retry", {}).get("polls", [{}])[-1]) == "Success"
        else "INCONCLUSIVE",
        "s4": "PASS"
        if _success(results["s4"].get("issue", {})) == "PASS"
        and results["s4"]["download"]["status"] < 300
        and results["s4"]["download"].get("size", 0) > 0
        else "FAIL",
        "s6": results["s6"]["scenario"],
    }
    return {
        "environment": "test",
        "generated_at": datetime.now(UTC).isoformat(),
        "scenarios": statuses,
        "details": {"s3_variant": s3_variant, **results},
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
    if name == "provider_account_detail":
        candidate = {"method": "GET", "path": "/v1/provider-accounts/{id}"}
    else:
        candidate = CANDIDATES[name]
    path = str(candidate["path"])
    for key, value in (path_values or {}).items():
        path = path.replace("{" + key + "}", value)
    if name == "download":
        status, body, headers = client.request_binary(str(candidate["method"]), path, params=params)
        return {"status": status, "body": body, "headers": headers}
    status, body, headers = client.request_json(
        str(candidate["method"]), path, payload=payload, params=params
    )
    return {"status": status, "body": body, "headers": headers}


def _invoice_payload(
    reference_code: str,
    tax_rate: str,
    issued_on: date,
    account: dict[str, str],
    *,
    tax_code: str | None = None,
) -> dict[str, Any]:
    rate = Decimal(tax_rate)
    buyer: dict[str, str] = {"name": "Probe Buyer", "email": "probe@example.com"}
    if tax_code:
        buyer["tax_code"] = tax_code
    unit_price = Decimal("1000")
    tax_amount = (
        int((unit_price * rate / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if rate > 0
        else 0
    )
    return {
        "reference_code": reference_code,
        "provider_account_id": account["provider_account_id"],
        "template_code": account["template_code"],
        "invoice_series": account["invoice_series"],
        "issued_date": issued_on.isoformat(),
        "buyer": buyer,
        "items": [
            {
                "line_number": 1,
                "line_type": 1,
                "item_name": "Probe item",
                "unit": "lần",
                "quantity": 1,
                "unit_price": int(unit_price),
                "tax_rate": tax_rate,
                "tax_amount": tax_amount,
            }
        ],
        "total_amount": int(unit_price + tax_amount),
    }


def _poll(client: ProbeClient, tracking_code: str) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for attempt in range(POLL_LIMIT):
        if attempt:
            time.sleep(POLL_INTERVAL_SECONDS)
        result = _call(client, "check_create", path_values={"tracking_code": tracking_code})
        states.append(result)
        if result["status"] >= 400 or _state(result) in {"Success", "Failed"}:
            break
    return states


def _find_provider_details(
    record: dict[str, Any], preferred_template: str | None = None
) -> dict[str, str]:
    provider_id = _value(record, "provider_account_id", "id")
    template = _value(record, "template_code")
    series = _value(record, "invoice_series")
    details = record.get("details", record.get("templates", []))
    if isinstance(details, dict):
        template = template or _value(details, "template_code")
        series = series or _value(details, "invoice_series")
    if isinstance(details, list):
        choices = [item for item in details if isinstance(item, dict)]
        if preferred_template:
            choices = [
                item for item in choices if _value(item, "template_code") == preferred_template
            ]
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
    if result.get("create", {}).get("status", 500) >= 400:
        return "FAIL"
    if not polls:
        return "INCONCLUSIVE"
    return (
        "PASS"
        if _state(polls[-1]) == "Success"
        else "FAIL"
        if _state(polls[-1]) == "Failed"
        else "INCONCLUSIVE"
    )


def _value(obj: Any, *keys: str) -> Any:
    if isinstance(obj, dict):
        normalized = {str(key).lower(): value for key, value in obj.items()}
        for key in keys:
            if key.lower() in normalized:
                return normalized[key.lower()]
    return None


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
