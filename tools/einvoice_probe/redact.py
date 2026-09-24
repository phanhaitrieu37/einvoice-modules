"""Remove credentials and personal data from probe output."""

from __future__ import annotations

import re
from collections.abc import Mapping

SECRET_KEYS = {"access_token", "token", "client_id", "client_secret", "authorization"}
PII_KEYS = {"name", "legal_name", "address", "email", "phone", "national_id"}
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def redact(obj: object) -> object:
    return _redact(obj, in_party=False)


def _redact(obj: object, *, in_party: bool) -> object:
    if isinstance(obj, Mapping):
        redacted: dict[object, object] = {}
        for key, value in obj.items():
            normalized = str(key).lower()
            if normalized in SECRET_KEYS or (normalized in PII_KEYS and in_party):
                redacted[key] = "REDACTED"
            elif normalized == "tax_code":
                redacted[key] = "0000000000"
            elif normalized == "provider_account_id":
                redacted[key] = "00000000-0000-0000-0000-000000000000"
            else:
                redacted[key] = _redact(
                    value, in_party=in_party or normalized in {"buyer", "seller"}
                )
        return redacted
    if isinstance(obj, list):
        return [_redact(value, in_party=in_party) for value in obj]
    if isinstance(obj, str):
        return JWT_RE.sub("REDACTED_JWT", obj)
    return obj
