from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from invoice_module.adapters.sepay import errors
from invoice_module.adapters.sepay.errors import map_error, retry_after
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
    assert isinstance(map_error(record["status"], record["body"]), DocumentExists)
    assert not isinstance(map_error(409, _error("OTHER")), DocumentExists)
    assert not isinstance(map_error(400, record["body"]), DocumentExists)


def test_quota_code_maps_to_quota_exhausted(
    recorded: Loader, monkeypatch: pytest.MonkeyPatch
) -> None:
    quota_code = recorded("findings.json")["quota_error_code"]
    assert quota_code == errors.QUOTA_ERROR_CODE
    if quota_code is None:
        documented = ["QUOTA_HAS_BEEN_USERD_UP", "QUOTA_EXHAUSTED", None]
        for status in (400, 402, 403, 409, 422, 429, 500):
            for code in documented:
                assert not isinstance(map_error(status, _error(code)), QuotaExhausted)
    else:
        assert isinstance(map_error(500, _error(quota_code)), QuotaExhausted)
    monkeypatch.setattr(errors, "QUOTA_ERROR_CODE", "SYNTHETIC_QUOTA")
    assert isinstance(map_error(500, _error("SYNTHETIC_QUOTA")), QuotaExhausted)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 307, 308, 301])
def test_5xx_and_timeout_map_to_transient(status: int) -> None:
    assert isinstance(map_error(status, _error("INTERNAL")), ProviderTransient)
    assert isinstance(map_error(status, None), ProviderTransient)


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
