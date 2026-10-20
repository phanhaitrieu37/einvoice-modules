"""Closed mapping from a SePay eInvoice error response to a port error.

Error envelope (docs ``xu-ly-loi``): ``{"success": false, "error": {"code", "message"}}``.
Codes come from the create docs (``xuat-hoa-don-dien-tu``) and the recorded sandbox calls.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from invoice_module.adapters.sepay.endpoints import QUOTA_ERROR_CODE
from invoice_module.ports.provider import (
    DocumentExists,
    ProviderAuthFailed,
    ProviderError,
    ProviderRejected,
    ProviderTransient,
    QuotaExhausted,
)

DOCUMENT_EXISTED = "EINVOICE_DOCUMENT_EXISTED"
ISSUED_DATE_INVALID = "ISSUED_DATE_INVALID"
ISSUED_DATE_INVALID_CODE = "issued_date_invalid"
# 5xx codes that are definitive: the company's e-invoice registration is not usable.
REGISTRATION_CODES = frozenset({"REGISTRATION_NOT_COMPLETE"})


def error_code(body: Any) -> str | None:
    error = body.get("error") if isinstance(body, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else None
    return code if isinstance(code, str) and code else None


def retry_after(headers: Mapping[str, str] | None, now: datetime | None = None) -> timedelta | None:
    """``Retry-After`` as seconds or an HTTP date; ``None`` when absent or unreadable."""
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(when - (now or datetime.now(UTC)), timedelta(0))


def map_error(status: int, body: Any, headers: Mapping[str, str] | None = None) -> ProviderError:
    """The port error for a non-success response.

    The client decides about a 401 on an API call before this mapping (a create is never
    resent); here 401 and 403 are authentication failures. Redirects are never followed,
    so 3xx is a transient outcome.
    """
    code = error_code(body)
    if status == 409 and code == DOCUMENT_EXISTED:
        return DocumentExists(DOCUMENT_EXISTED)
    if QUOTA_ERROR_CODE is not None and code == QUOTA_ERROR_CODE:
        return QuotaExhausted(code)
    if status in (401, 403):
        return ProviderAuthFailed(code or f"http_{status}")
    if status == 429 or 300 <= status < 400 or (status >= 500 and code not in REGISTRATION_CODES):
        return ProviderTransient(retry_after(headers))
    if status == 400 and code == ISSUED_DATE_INVALID:
        return ProviderRejected(ISSUED_DATE_INVALID_CODE)
    return ProviderRejected(code or f"http_{status}")
