import dataclasses
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest

from invoice_module.ports.provider import (
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderTransient,
)

pytestmark = pytest.mark.contract

REFERENCE = "00000000-0000-4000-8000-00000000c0de"
Loader = Callable[[str], dict[str, Any]]


def _unauthorized() -> httpx.Response:
    return httpx.Response(401, json={"success": False, "error": {"code": "UNAUTHORIZED"}})


async def test_token_reused_within_ttl(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    h = harness(lambda request: httpx.Response(200, json=success))
    await h.provider.check_create(account, creds, REFERENCE)
    h.clock.advance(timedelta(hours=23, minutes=54))
    await h.provider.check_create(account, creds, REFERENCE)
    tokens = [r for r in h.requests if r.url.path == "/v1/token"]
    assert len(tokens) == 1
    assert tokens[0].method == "POST"
    assert tokens[0].headers["Authorization"].startswith("Basic ")
    gets = h.calls("/check/")
    assert {r.headers["Authorization"] for r in gets} == {"Bearer synthetic-access-token-value-1"}


async def test_token_refreshed_near_expiry(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    h = harness(lambda request: httpx.Response(200, json=success))
    await h.provider.check_create(account, creds, REFERENCE)
    h.clock.advance(timedelta(hours=23, minutes=55))
    await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 2
    assert [r.headers["Authorization"] for r in h.calls("/check/")] == [
        "Bearer synthetic-access-token-value-1",
        "Bearer synthetic-access-token-value-2",
    ]
    tokens = [i for i, event in enumerate(h.events) if event == "token"]
    gets = [i for i, event in enumerate(h.events) if event.startswith("GET ")]
    assert tokens[0] < gets[0] < tokens[1] < gets[1]


async def test_token_cache_is_per_credential_ref(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    h = harness(lambda request: httpx.Response(200, json=success))
    other = dataclasses.replace(account, credential_ref="env:OTHER_SEPAY")
    await h.provider.check_create(account, creds, REFERENCE)
    await h.provider.check_create(other, creds, REFERENCE)
    await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 2


async def test_create_401_invalidates_token_without_resend(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _unauthorized()
        return httpx.Response(200, json=success)

    h = harness(handler)
    await h.provider.check_create(account, creds, REFERENCE)
    with pytest.raises(ProviderTransient) as cached:
        await h.provider.create(account, creds, {"reference_code": REFERENCE})
    assert cached.value.retry_after is None
    assert len(h.calls("/v1/invoices/create", "POST")) == 1
    assert h.events.count("token") == 1
    await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 2
    assert h.calls("/check/")[-1].headers["Authorization"].endswith("-2")

    fresh = harness(handler)
    with pytest.raises(ProviderAuthFailed):
        await fresh.provider.create(account, creds, {"reference_code": REFERENCE})
    assert len(fresh.calls("/v1/invoices/create", "POST")) == 1
    assert fresh.events.count("token") == 1


async def test_get_401_refreshes_once_then_auth_failed(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    answers = iter([_unauthorized(), httpx.Response(200, json=success)])
    h = harness(lambda request: next(answers))
    progress = await h.provider.check_create(account, creds, REFERENCE)
    assert progress.state == "success"
    assert h.events.count("token") == 2
    assert len(h.calls("/check/")) == 2

    always = harness(lambda request: _unauthorized())
    with pytest.raises(ProviderAuthFailed):
        await always.provider.check_create(account, creds, REFERENCE)
    assert len(always.calls("/check/")) == 2
    assert always.events.count("token") == 2


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, ProviderAuthFailed), (403, ProviderAuthFailed), (503, ProviderTransient)],
)
async def test_token_endpoint_failure_is_mapped_and_nothing_else_is_sent(
    status: int,
    error: type[Exception],
    account: IssuerAccountView,
    creds: Any,
    harness: Callable[..., Any],
) -> None:
    h = harness(
        lambda request: httpx.Response(200, json={}),
        token_handler=lambda request: httpx.Response(status, json={"success": False}),
    )
    with pytest.raises(error):
        await h.provider.check_create(account, creds, REFERENCE)
    assert [r.url.path for r in h.requests] == ["/v1/token"]


async def test_authorization_never_logged(
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    success = recorded("check_success.json")["body"]
    answers = iter([_unauthorized(), httpx.Response(200, json=success)])
    h = harness(lambda request: next(answers) if request.method == "GET" else _unauthorized())
    await h.provider.check_create(account, creds, REFERENCE)
    with pytest.raises(ProviderTransient):
        await h.provider.create(account, creds, {"reference_code": REFERENCE})
    text = caplog.text + "".join(repr(record.args) for record in caplog.records)
    assert caplog.records
    assert "synthetic-access-token-value" not in text
    assert "Bearer" not in text
    assert "synthetic-client-secret" not in text
    assert "Authorization" not in text


async def test_failure_tracebacks_never_show_the_token(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/download"):
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=success)

    h = harness(handler)
    await h.provider.check_create(account, creds, REFERENCE)
    with pytest.raises(ProviderTransient) as raised:
        await h.provider.fetch_artifacts(account, creds, REFERENCE, "unused")
    frames = [
        entry
        for error in (raised.value, raised.value.__cause__)
        for entry in pytest.ExceptionInfo.from_exception(error).traceback
        if "invoice_module" in str(entry.path)
    ]
    shown = "\n".join(f"{name} = {value!r}" for f in frames for name, value in f.frame.getargs())
    assert "token = _Token(<hidden>)" in shown
    assert "synthetic-access-token-value" not in shown
