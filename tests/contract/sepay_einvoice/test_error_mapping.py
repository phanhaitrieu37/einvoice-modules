from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from invoice_module.adapters.sepay.errors import error_code, map_error, retry_after
from invoice_module.ports.provider import (
    DocumentExists,
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderRejected,
    ProviderTransient,
    QuotaExhausted,
)

pytestmark = pytest.mark.contract

Loader = Callable[[str], dict[str, Any]]
REFERENCE = "00000000-0000-4000-8000-00000000c0de"


def _error(code: str | None) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": "synthetic"}}


def test_409_maps_to_document_exists(recorded: Loader) -> None:
    record = recorded("create_409.json")
    existed = map_error(record["status"], record["body"])
    assert isinstance(existed, DocumentExists)
    assert str(existed) == "EINVOICE_DOCUMENT_EXISTED"
    other = map_error(409, _error("OTHER"))
    assert isinstance(other, DocumentExists) and str(other) == "OTHER"
    for body in (None, {}, "<html>conflict</html>", _error(None)):
        bare = map_error(409, body)
        assert isinstance(bare, DocumentExists) and str(bare) == "http_409"
    assert not isinstance(map_error(400, record["body"]), DocumentExists)
    assert not isinstance(map_error(500, record["body"]), DocumentExists)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (500, "QUOTA_HAS_BEEN_USERD_UP", QuotaExhausted),
        (400, "QUOTA_HAS_BEEN_USERD_UP", QuotaExhausted),
        (429, "QUOTA_HAS_BEEN_USERD_UP", QuotaExhausted),
        (500, "REGISTRATION_NOT_COMPLETE", ProviderRejected),
        (500, None, ProviderTransient),
        (500, "INTERNAL_SERVER_ERROR", ProviderTransient),
        (429, "TOO_MANY_REQUESTS", ProviderTransient),
        (429, None, ProviderTransient),
        (500, "QUOTA_HAS_BEEN_USED_UP", ProviderTransient),
        (500, "quota_has_been_userd_up", ProviderTransient),
    ],
)
def test_quota_code_maps_to_quota_exhausted(
    status: int, code: str | None, expected: type[Exception]
) -> None:
    error = map_error(status, _error(code))
    assert type(error) is expected
    if expected is QuotaExhausted:
        assert str(error) == "QUOTA_HAS_BEEN_USERD_UP"


@pytest.mark.parametrize(
    "code", ["REGISTRATION_NOT_COMPLETE", "BILLING_IS_UNPAID", "EINVOICE_ACCOUNT_NOT_FOUND"]
)
@pytest.mark.parametrize("status", [500, 503])
def test_account_status_5xx_codes_are_definitive(status: int, code: str) -> None:
    error = map_error(status, _error(code))
    assert isinstance(error, ProviderRejected)
    assert error.code == code


@pytest.mark.parametrize("code", ["CREATE_INVOICE_DATA_FAIL", "INTERNAL_SERVER_ERROR", None])
def test_unrecognised_500_is_a_system_error(code: str | None) -> None:
    assert isinstance(map_error(500, _error(code)), ProviderTransient)


@pytest.mark.parametrize("status", [429, 500, 501, 502, 503, 504, 307, 308, 301])
def test_5xx_and_timeout_map_to_transient(status: int) -> None:
    assert isinstance(map_error(status, _error("INTERNAL")), ProviderTransient)
    assert isinstance(map_error(status, None), ProviderTransient)


def test_error_code_needs_a_non_empty_string() -> None:
    assert error_code(_error("")) is None
    assert error_code(_error(None)) is None
    assert error_code({"error": {"code": 7}}) is None
    assert error_code({"error": "VALIDATION_ERROR"}) is None
    assert error_code("VALIDATION_ERROR") is None
    assert error_code(_error("VALIDATION_ERROR")) == "VALIDATION_ERROR"
    assert map_error(422, _error("")).code == "http_422"


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.ConnectError("x")]
)
async def test_network_errors_map_to_transient(
    error: Exception, harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    h = harness(handler)
    with pytest.raises(ProviderTransient) as raised:
        await h.provider.check_create(account, creds, REFERENCE)
    assert raised.value.retry_after is None


def test_422_maps_to_rejected_with_code(recorded: Loader) -> None:
    rejected = map_error(422, _error("REGISTRATION_NOT_TAX_APPROVED"))
    assert isinstance(rejected, ProviderRejected)
    assert rejected.code == "REGISTRATION_NOT_TAX_APPROVED"
    record = recorded("error_422.json")
    recorded_error = map_error(record["status"], record["body"])
    assert isinstance(recorded_error, ProviderRejected)
    assert recorded_error.code == "INVALID_IS_DRAFT_TYPE"
    validation = map_error(400, _error("VALIDATION_ERROR"))
    assert isinstance(validation, ProviderRejected) and validation.code == "VALIDATION_ERROR"
    registration = map_error(500, _error("REGISTRATION_NOT_COMPLETE"))
    assert isinstance(registration, ProviderRejected)
    assert registration.code == "REGISTRATION_NOT_COMPLETE"
    no_code = map_error(422, {"success": False})
    assert isinstance(no_code, ProviderRejected) and no_code.code == "http_422"


def test_401_and_403_map_to_auth_failed(recorded: Loader) -> None:
    record = recorded("error_401.json")
    assert isinstance(map_error(record["status"], record["body"]), ProviderAuthFailed)
    assert isinstance(map_error(403, _error("FORBIDDEN")), ProviderAuthFailed)


def test_retry_after_parsed() -> None:
    now = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
    assert retry_after({"retry-after": "30"}) == timedelta(seconds=30)
    assert retry_after({"Retry-After": "7"}) == timedelta(seconds=7)
    http_date = {"retry-after": "Tue, 20 Oct 2026 03:01:30 GMT"}
    assert retry_after(http_date, now) == timedelta(seconds=90)
    assert retry_after({"retry-after": "soon"}) is None
    assert retry_after({}) is None
    assert retry_after(None) is None
    transient = map_error(429, _error("TOO_MANY_REQUESTS"), {"retry-after": "12"})
    assert isinstance(transient, ProviderTransient)
    assert transient.retry_after == timedelta(seconds=12)
    assert map_error(503, None, {"retry-after": "5"}).retry_after == timedelta(seconds=5)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\u00b2", None),
        ("\u0663", None),
        ("9" * 20, timedelta(days=1)),
        ("9" * 5000, timedelta(days=1)),
        ("86400", timedelta(days=1)),
        ("86401", timedelta(days=1)),
        ("-5", None),
        (" 12 ", timedelta(seconds=12)),
        ("1.5", None),
        ("Tue, 20 Oct 2026 03:01:30 GMT", timedelta(seconds=90)),
        ("Mon, 19 Oct 2026 03:00:00 GMT", timedelta(0)),
        ("Fri, 31 Dec 9999 23:59:59 GMT", timedelta(days=1)),
        ("Tue, 20 Oct 2026 03:01:30", None),
        ("Tue, 99 Oct 2026 03:01:30 GMT", None),
        ("", None),
    ],
)
def test_retry_after_never_raises(value: str, expected: timedelta | None) -> None:
    now = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
    assert retry_after({"retry-after": value}, now) == expected
    error = map_error(503, None, {"retry-after": value})
    assert isinstance(error, ProviderTransient)


async def test_retry_after_read_from_response_headers(
    harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    h = harness(lambda request: httpx.Response(429, headers={"Retry-After": "9"}, json={}))
    with pytest.raises(ProviderTransient) as raised:
        await h.provider.check_create(account, creds, REFERENCE)
    assert raised.value.retry_after == timedelta(seconds=9)


def test_issued_date_invalid_maps_to_rejected_issued_date_invalid(synthetic: Loader) -> None:
    record = synthetic("create_issued_date_invalid.json")
    assert record["_synthetic"]
    error = map_error(record["status"], record["body"])
    assert isinstance(error, ProviderRejected)
    assert error.code == "issued_date_invalid"
    assert map_error(422, record["body"]).code == "ISSUED_DATE_INVALID"


async def test_create_validation_error_is_a_definitive_rejection(
    harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    body = _error("VALIDATION_ERROR")
    h = harness(lambda request: httpx.Response(400, json=body))
    with pytest.raises(ProviderRejected) as raised:
        await h.provider.create(account, creds, {"items": [{"item_name": "x"}]})
    assert raised.value.code == "VALIDATION_ERROR"
    assert len(h.calls("/v1/invoices/create", "POST")) == 1
