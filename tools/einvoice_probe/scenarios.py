"""Probe workflows; each response is redacted before it is returned or saved."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .candidates import CANDIDATES
from .client import ProbeClient
from .redact import redact


def accounts(client: ProbeClient) -> dict[str, Any]:
    return _call(client, "provider_accounts")


def s1_create_poll(client: ProbeClient, *, tax_rate: int) -> dict[str, Any]:
    ref = str(uuid4())
    created = _call(client, "create", payload=_invoice_payload(ref, tax_rate))
    result: dict[str, Any] = {"create": created}
    body = created.get("body", {})
    tracking = _value(body, "tracking_code", "trackingCode", "id")
    if created["status"] < 400 and tracking:
        result["check"] = _call(
            client, "check_create", path_values={"tracking_code": str(tracking)}
        )
    result["reference_code"] = ref
    return result


def s2_duplicate_reference(client: ProbeClient, *, tax_rate: int) -> dict[str, Any]:
    ref = str(uuid4())
    first = _call(client, "create", payload=_invoice_payload(ref, tax_rate))
    duplicate = _call(client, "create", payload=_invoice_payload(ref, tax_rate))
    lookup = _call(client, "get_by_reference", path_values={"reference_code": ref})
    return {"first": first, "duplicate": duplicate, "lookup": lookup, "reference_code": ref}


def s3_failed_then_retry(client: ProbeClient, *, tax_rate: int) -> dict[str, Any]:
    ref = str(uuid4())
    invalid_tax_code = "9999999999"
    created = _call(
        client, "create", payload=_invoice_payload(ref, tax_rate, tax_code=invalid_tax_code)
    )
    if created["status"] >= 400:
        variant = "sync_reject"
        retry = None
        check = None
    else:
        tracking = _value(created.get("body", {}), "tracking_code", "trackingCode", "id")
        check = (
            _call(client, "check_create", path_values={"tracking_code": str(tracking)})
            if tracking
            else None
        )
        state = str(
            _value(check.get("body", {}) if check else {}, "status", "state", "result") or ""
        ).lower()
        if state == "failed":
            variant = "async_failed"
            retry = _call(client, "create", payload=_invoice_payload(ref, tax_rate))
        else:
            variant = "not_triggerable"
            retry = None
    return {
        "variant": variant,
        "create": created,
        "check": check,
        "retry": retry,
        "reference_code": ref,
    }


def s4_download(client: ProbeClient, *, tax_rate: int) -> dict[str, Any]:
    issued = s1_create_poll(client, tax_rate=tax_rate)
    ref = issued.get("reference_code", "")
    metadata = _call(client, "download", path_values={"id": ref, "reference_code": ref})
    return {"issue": issued, "download": metadata}


def s6_quota(client: ProbeClient) -> dict[str, Any]:
    return _call(client, "quota")


def evidence(client: ProbeClient, *, tax_rate: int) -> dict[str, Any]:
    results = {
        "s1": s1_create_poll(client, tax_rate=tax_rate),
        "s2": s2_duplicate_reference(client, tax_rate=tax_rate),
        "s3": s3_failed_then_retry(client, tax_rate=tax_rate),
        "s4": s4_download(client, tax_rate=tax_rate),
        "s6": s6_quota(client),
    }
    s3_variant = results["s3"]["variant"]
    statuses = {
        "s1": _success(results["s1"]),
        "s2": "PASS"
        if results["s2"]["duplicate"]["status"] == 409 and results["s2"]["lookup"]["status"] < 400
        else "FAIL",
        "s3": "PASS"
        if s3_variant == "async_failed" and results["s3"].get("retry", {}).get("status", 500) < 400
        else "INCONCLUSIVE",
        "s4": _success(results["s4"].get("issue", {})),
        "s6": "PASS" if results["s6"]["status"] < 400 else "INCONCLUSIVE",
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
    path.write_text(json.dumps(redact(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _call(
    client: ProbeClient,
    name: str,
    *,
    payload: dict[str, Any] | None = None,
    path_values: dict[str, str] | None = None,
) -> dict[str, Any]:
    candidate = CANDIDATES[name]
    path = str(candidate["path"])
    for key, value in (path_values or {}).items():
        path = path.replace("{" + key + "}", value)
    status, body, headers = client.request_json(str(candidate["method"]), path, payload=payload)
    return {
        "status": status,
        "body": redact(body),
        "headers": {"content-type": headers.get("content-type")},
    }


def _invoice_payload(
    reference_code: str, tax_rate: int, *, tax_code: str | None = None
) -> dict[str, Any]:
    buyer: dict[str, str] = {"name": "Probe Buyer", "email": "probe@example.com"}
    if tax_code:
        buyer["tax_code"] = tax_code
    return {
        "reference_code": reference_code,
        "buyer": buyer,
        "items": [
            {
                "name": "Probe item",
                "unit": "lần",
                "quantity": 1,
                "unit_price": 1000,
                "tax_rate": tax_rate,
            }
        ],
        "total_amount": 1000,
    }


def _value(obj: Any, *keys: str) -> Any:
    if isinstance(obj, dict):
        for key in keys:
            if key in obj:
                return obj[key]
    return None


def _success(result: dict[str, Any]) -> str:
    if result.get("status", 500) >= 400:
        return "FAIL"
    return "PASS" if result.get("check", {}).get("status", 500) < 400 else "INCONCLUSIVE"
