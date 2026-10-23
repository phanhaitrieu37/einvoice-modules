"""Closed mapping from a SePay eInvoice error response to a port error.

Error envelope (docs ``xu-ly-loi``): ``{"success": false, "error": {"code", "message"}}``.
Codes come from the error and create docs (``xu-ly-loi``, ``xuat-hoa-don-dien-tu``) and the
recorded sandbox calls. Errors are told apart by ``error.code`` only, never by the message
text ("Xử lý lỗi dựa trên error.code, không khớp theo chuỗi error.message").
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from invoice_module.ports.provider import (
    DocumentExists,
    ProviderAuthFailed,
    ProviderError,
    ProviderRejected,
    ProviderTransient,
    QuotaExhausted,
)

ISSUED_DATE_INVALID = "ISSUED_DATE_INVALID"
ISSUED_DATE_INVALID_CODE = "issued_date_invalid"
# Docs ``xu-ly-loi``: "QUOTA_HAS_BEEN_USERD_UP viết đúng như API trả về, kể cả lỗi chính tả
# trong mã". The provider answers it with HTTP 500 and it does not clear by itself.
QUOTA_CODE = "QUOTA_HAS_BEEN_USERD_UP"
# 5xx codes about the account status: "API này trả 500 cho cả các lỗi trạng thái tài khoản
# ... gọi lại không làm thay đổi kết quả. Chỉ 500 không kèm mã lỗi nhận diện được mới là lỗi
# hệ thống" (docs ``xu-ly-loi``; ``EINVOICE_ACCOUNT_NOT_FOUND`` is a wrong
# ``provider_account_id``).
ACCOUNT_STATUS_CODES = frozenset(
    {"REGISTRATION_NOT_COMPLETE", "BILLING_IS_UNPAID", "EINVOICE_ACCOUNT_NOT_FOUND"}
)
# Longest ``Retry-After`` honoured, in seconds; the docs do not document the header at all.
MAX_RETRY_AFTER_S = 86400


def error_code(body: Any) -> str | None:
    error = body.get("error") if isinstance(body, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else None
    return code if isinstance(code, str) and code else None


def retry_after(headers: Mapping[str, str] | None, now: datetime | None = None) -> timedelta | None:
    """``Retry-After`` as seconds or an HTTP date, at most one day; ``None`` when unreadable."""
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if not value:
        return None
    value = value.strip()
    longest = timedelta(seconds=MAX_RETRY_AFTER_S)
    if value.isascii() and value.isdigit():
        if len(value) > len(str(MAX_RETRY_AFTER_S)):
            return longest
        return min(timedelta(seconds=int(value)), longest)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        return None
    return min(max(when - (now or datetime.now(UTC)), timedelta(0)), longest)


def map_error(status: int, body: Any, headers: Mapping[str, str] | None = None) -> ProviderError:
    """The port error for a non-success response.

    The client decides about a 401 on an API call before this mapping (a create is never
    resent); here 401 and 403 are authentication failures. Redirects are never followed,
    so 3xx is a transient outcome. Any 409 means the object already exists ("đối tượng đã
    tồn tại hoặc đang được sử dụng"); the caller looks the reference up.
    """
    code = error_code(body)
    if status == 409:
        return DocumentExists(code or f"http_{status}")
    if code == QUOTA_CODE:
        return QuotaExhausted(code)
    if status in (401, 403):
        return ProviderAuthFailed(code or f"http_{status}")
    if status >= 500 and code in ACCOUNT_STATUS_CODES:
        return ProviderRejected(code)
    if status == 429 or 300 <= status < 400 or status >= 500:
        return ProviderTransient(retry_after(headers))
    if status == 400 and code == ISSUED_DATE_INVALID:
        return ProviderRejected(ISSUED_DATE_INVALID_CODE)
    return ProviderRejected(code or f"http_{status}")
