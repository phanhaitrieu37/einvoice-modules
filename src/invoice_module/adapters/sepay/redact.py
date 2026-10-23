"""Mask buyer personal and tax data before an adapter log line or error detail is written.

A key is judged by its normalised form: ``buyerTaxCode``, ``BuyerTaxCode`` and
``buyer_tax_code`` are the same key, a key inside a ``buyer`` mapping counts as
``buyer_<key>``, and a ``{"path": ..., "value": ...}`` descriptor is judged by the tail of
its dotted path the same way. Blank (``""``) and null values carry no data and stay as they
are, so an empty buyer address remains visible as evidence.
"""

import re
from collections.abc import Mapping
from typing import Any

BUYER_PREFIX = "buyer_"
BUYER_TAX_SUFFIXES = frozenset({"tax_code"})
BUYER_PII_SUFFIXES = frozenset(
    {
        "name",
        "legal_name",
        "address",
        "email",
        "phone",
        "phone_number",
        "id_number",
        "identity_number",
        "citizen_id",
        "national_id",
        "bank_account",
    }
)
TAX_MASK = "0000000000"
PII_MASK = "REDACTED"

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def normalise_key(key: str) -> str:
    """camelCase or PascalCase to lower snake_case; snake_case is unchanged."""
    return _CAMEL_BOUNDARY.sub("_", key).lower()


def redact(obj: Any) -> Any:
    """A copy of ``obj`` with every sensitive buyer value masked."""
    return _walk(obj, parent=None)


def _effective_key(key: str, parent: str | None) -> str:
    normalised = normalise_key(key)
    return f"{BUYER_PREFIX}{normalised}" if parent == "buyer" else normalised


def _mask_for(key: str) -> str | None:
    if not key.startswith(BUYER_PREFIX):
        return None
    if any(key.endswith(f"_{suffix}") for suffix in BUYER_TAX_SUFFIXES):
        return TAX_MASK
    if any(key.endswith(f"_{suffix}") for suffix in BUYER_PII_SUFFIXES):
        return PII_MASK
    return None


def _masked(value: Any, mask: str) -> Any:
    if value is None or value == "":
        return value
    return mask


def _descriptor_key(path: str) -> str:
    segments = [segment for segment in path.split(".") if segment]
    if not segments:
        return ""
    parent = normalise_key(segments[-2]) if len(segments) > 1 else None
    return _effective_key(segments[-1], parent)


def _walk(obj: Any, parent: str | None) -> Any:
    if isinstance(obj, Mapping):
        result: dict[Any, Any] = {}
        for key, value in obj.items():
            effective = _effective_key(key, parent) if isinstance(key, str) else None
            mask = _mask_for(effective) if effective is not None else None
            if mask is not None:
                result[key] = _masked(value, mask)
            else:
                result[key] = _walk(value, effective)
        path = obj.get("path")
        if isinstance(path, str) and "value" in obj:
            mask = _mask_for(_descriptor_key(path))
            if mask is not None:
                result["value"] = _masked(obj["value"], mask)
        return result
    if isinstance(obj, list | tuple):
        return [_walk(item, parent) for item in obj]
    return obj
