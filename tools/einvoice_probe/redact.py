"""Remove credentials and personal data from probe output."""

from __future__ import annotations

import re
from collections.abc import Mapping

SECRET_KEYS = {"access_token", "token", "client_id", "client_secret", "authorization"}
PII_KEYS = {"name", "legal_name", "address", "email", "phone", "national_id"}
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
TAX_ID_TEXT_RE = re.compile(r"(?<!\d)(?:\d{10}|\d{13})(?!\d)")
URL_QUERY_RE = re.compile(r"(?i)(https?://[^\s\"'<>?#]+)\?[^\s\"'<>#]*")


def redact(obj: object) -> object:
    return _redact(obj, in_party=False)


def _redact(obj: object, *, in_party: bool, field_name: str | None = None) -> object:
    if isinstance(obj, Mapping):
        redacted: dict[object, object] = {}
        for key, value in obj.items():
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
            if normalized == "address" and field_name == "buyer" and _is_blank(value):
                # A blank fallback buyer address is evidence, not personal data.
                redacted[key] = value
            elif normalized == "buyer_address":
                # A flat buyer address outside a buyer object follows the same rule.
                redacted[key] = value if _is_blank(value) else "REDACTED"
            elif (
                normalized in SECRET_KEYS
                and not (normalized == "token" and _is_endpoint_descriptor(value))
            ) or (normalized in PII_KEYS and in_party):
                redacted[key] = "REDACTED"
            elif normalized == "tax_code":
                redacted[key] = "0000000000"
            elif normalized == "provider_account_id" or (normalized == "id" and "provider" in obj):
                redacted[key] = "00000000-0000-0000-0000-000000000000"
            else:
                redacted[key] = _redact(
                    value,
                    in_party=in_party or normalized in {"buyer", "seller", "seller_store"},
                    field_name=normalized,
                )
        mask = _field_descriptor_mask(obj)
        if mask is not None:
            # Siblings are redacted like any other key; only the field's own value is masked.
            redacted["value"] = mask
        return redacted
    if isinstance(obj, list):
        return [_redact(value, in_party=in_party) for value in obj]
    if isinstance(obj, str):
        cleaned = JWT_RE.sub("REDACTED_JWT", obj)
        if field_name in {"message", "description", "error", "details", "note", "remark"}:
            cleaned = TAX_ID_TEXT_RE.sub("0000000000", cleaned)
        return URL_QUERY_RE.sub(r"\1?REDACTED", cleaned)
    return obj


def _is_blank(value: object) -> bool:
    return value is None or value == ""


def _field_descriptor_mask(value: Mapping[object, object]) -> str | None:
    """The mask for a ``{path, value}`` finding whose path names a personal-data or tax field."""
    path = value.get("path")
    if not isinstance(path, str) or "value" not in value or _is_blank(value["value"]):
        return None
    field = path.rsplit(".", 1)[-1].lower()
    if field == "tax_code":
        return "0000000000"
    if field in PII_KEYS or field.endswith("_address"):
        return "REDACTED"
    return None


def _is_endpoint_descriptor(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"method", "path", "verified"}
        and isinstance(value.get("method"), str)
        and isinstance(value.get("verified"), bool)
    )
