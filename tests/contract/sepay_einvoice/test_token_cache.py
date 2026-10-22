import asyncio
import dataclasses
import logging
import traceback
from collections.abc import Callable, Iterator
from datetime import timedelta
from types import FrameType
from typing import Any

import httpx
import pytest

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.domain.enums import Environment
from invoice_module.ports.clock import SystemClock
from invoice_module.ports.provider import (
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderRejected,
    ProviderTransient,
)
from invoice_module.secrets import EnvCredentialResolver

pytestmark = pytest.mark.contract

REFERENCE = "00000000-0000-4000-8000-00000000c0de"
# Prefix of every synthetic token the harness issues, and the synthetic client secret.
TOKEN_VALUE = "synthetic-access-token-value"
SECRETS = (TOKEN_VALUE, "synthetic-client-secret")
Loader = Callable[[str], dict[str, Any]]


def token_response(value: str, expires_in: int) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "data": {"access_token": value, "expires_in": expires_in}}
    )


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


def _chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _frames(error: BaseException) -> Iterator[FrameType]:
    for link in _chain(error):
        tb = link.__traceback__
        while tb is not None:
            yield tb.tb_frame
            tb = tb.tb_next


def assert_no_secret_in_any_frame(error: BaseException) -> list[str]:
    """Every local of every frame of the exception chain, library frames included.

    Returns the file of each inspected frame.
    """
    leaks: list[str] = []
    files: list[str] = []
    for frame in _frames(error):
        files.append(frame.f_code.co_filename)
        for name, value in frame.f_locals.items():
            if any(secret in repr(value) for secret in SECRETS):
                leaks.append(f"{frame.f_code.co_filename}:{frame.f_code.co_name}:{name}")
    assert leaks == []
    rendered = [
        "".join(traceback.TracebackException.from_exception(error, capture_locals=True).format()),
        str(
            pytest.ExceptionInfo.from_exception(error).getrepr(
                style="long", showlocals=True, funcargs=True, chain=True
            )
        ),
    ]
    for text in rendered:
        assert all(secret not in text for secret in SECRETS)
    return files


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


def _token_with_ttl(expires_in: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        200, json={"success": True, "data": {"access_token": TOKEN_VALUE, "expires_in": expires_in}}
    )


FRAME_CASES = {
    "create_timeout_cached_token": ("create", _timeout, None, True, ProviderTransient),
    "create_timeout_fresh_token": ("create", _timeout, None, False, ProviderTransient),
    "create_timeout_overflowing_ttl": (
        "create",
        _timeout,
        _token_with_ttl(10**12),
        False,
        ProviderTransient,
    ),
    "create_400_cached_token": (
        "create",
        lambda request: httpx.Response(400, json={"success": False, "error": {"code": "X"}}),
        None,
        True,
        ProviderRejected,
    ),
    "check_timeout": ("check", _timeout, None, False, ProviderTransient),
    "download_timeout_cached_token": ("download", _timeout, None, True, ProviderTransient),
}


@pytest.mark.parametrize("case", sorted(FRAME_CASES))
async def test_token_absent_from_every_frame_local_after_failure(
    case: str,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    call, failing, token_handler, warm, error = FRAME_CASES[case]
    success = recorded("check_success.json")["body"]
    targets = {
        "check": lambda request: "/check/" in request.url.path,
        "create": lambda request: request.method == "POST",
        "download": lambda request: request.url.path.endswith("/download"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if targets[call](request):
            return failing(request)
        return httpx.Response(200, json=success)

    h = harness(handler, token_handler=token_handler)
    if warm:
        await h.provider.check_create(account, creds, REFERENCE)
    with pytest.raises(error) as raised:
        if call == "create":
            await h.provider.create(account, creds, {"reference_code": REFERENCE})
        elif call == "check":
            await h.provider.check_create(account, creds, REFERENCE)
        else:
            await h.provider.fetch_artifacts(account, creds, REFERENCE, "unused")
    assert h.events.count("token") == 1
    files = assert_no_secret_in_any_frame(raised.value)
    assert any("/invoice_module/adapters/sepay/" in file for file in files)
    if error is ProviderTransient:
        assert any("/httpx/" in file for file in files)


_SOCKET_TOKEN_BODY = (
    b'{"success": true, "data": {"access_token": "' + TOKEN_VALUE.encode() + b'-socket", '
    b'"expires_in": 86400}}'
)


async def test_token_absent_from_frames_over_a_real_socket(
    account: IssuerAccountView, creds: Any
) -> None:
    """A create that times out on a loopback socket: httpcore and h11 frames are inspected."""
    received: list[bytes] = []
    release = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.lower().split(b"\r\n")
            length = next(
                (int(x.split(b":")[1]) for x in lines if x.startswith(b"content-length:")), 0
            )
            await reader.readexactly(length)
            received.append(head.split(b" ")[1])
            if not head.startswith(b"POST /v1/token "):
                await release.wait()
                writer.close()
                return
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                + b"content-length: %d\r\n\r\n" % len(_SOCKET_TOKEN_BODY)
                + _SOCKET_TOKEN_BODY
            )
            await writer.drain()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    live = dataclasses.replace(account, environment=Environment.LIVE)
    try:
        async with httpx.AsyncClient() as http:
            client = SePayEInvoiceClient(
                http,
                EnvCredentialResolver({}),
                SystemClock(),
                ProcessRateLimiter(1000.0),
                base_urls={Environment.LIVE: f"http://127.0.0.1:{port}"},
                http_timeout_s=0.5,
            )
            provider = SePayEInvoiceProvider(client)
            with pytest.raises(ProviderTransient) as raised:
                await provider.create(live, creds, {"reference_code": REFERENCE})
    finally:
        release.set()
        server.close()
    assert received == [b"/v1/token", b"/v1/invoices/create"]
    files = assert_no_secret_in_any_frame(raised.value)
    assert any("/httpcore/" in file for file in files)


@pytest.mark.parametrize(
    "expires_in", [10**12, 365 * 86400 + 1, 0, -5, "86400", True, None, 1.5, "absent"]
)
async def test_overflowing_expires_in_falls_back_to_ttl(
    expires_in: object,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    """An unbelievable or missing ``expires_in`` caches the token for the documented 24 h."""
    data: dict[str, object] = {"access_token": f"{TOKEN_VALUE}-x"}
    if expires_in != "absent":
        data["expires_in"] = expires_in
    success = recorded("check_success.json")["body"]
    h = harness(
        lambda request: httpx.Response(200, json=success),
        token_handler=lambda request: httpx.Response(200, json={"success": True, "data": data}),
    )
    await h.provider.check_create(account, creds, REFERENCE)
    h.clock.advance(timedelta(hours=23, minutes=54))
    await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 1
    h.clock.advance(timedelta(minutes=1))
    await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 2


async def test_longest_believed_expires_in_is_one_year(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    h = harness(
        lambda request: httpx.Response(200, json=success),
        token_handler=lambda request: token_response(f"{TOKEN_VALUE}-x", 365 * 86400),
    )
    await h.provider.check_create(account, creds, REFERENCE)
    h.clock.advance(timedelta(days=300))
    await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 1


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")]
)
async def test_token_endpoint_network_error_is_transient(
    error: Exception, harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    def token_handler(request: httpx.Request) -> httpx.Response:
        raise error

    h = harness(lambda request: httpx.Response(200, json={}), token_handler=token_handler)
    with pytest.raises(ProviderTransient) as raised:
        await h.provider.create(account, creds, {"reference_code": REFERENCE})
    assert raised.value.retry_after is None
    assert [r.url.path for r in h.requests] == ["/v1/token"]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_token_request_never_follows_a_redirect(
    status: int, harness: Callable[..., Any], account: IssuerAccountView, creds: Any
) -> None:
    location = "https://einvoice-api-sandbox.sepay.vn/v1/token"
    h = harness(
        lambda request: httpx.Response(200, json={}),
        token_handler=lambda request: httpx.Response(status, headers={"Location": location}),
    )
    with pytest.raises(ProviderTransient):
        await h.provider.check_create(account, creds, REFERENCE)
    assert [(r.method, r.url.path) for r in h.requests] == [("POST", "/v1/token")]


async def test_get_401_retry_goes_through_the_limiter(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    answers = iter([_unauthorized(), httpx.Response(200, json=success)])
    h = harness(lambda request: next(answers))
    await h.provider.check_create(account, creds, REFERENCE)
    get = f"GET /v1/invoices/create/check/{REFERENCE}"
    assert h.events == ["limiter", "token", "limiter", get, "limiter", "token", "limiter", get]


async def test_second_get_401_drops_the_rejected_token(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    answers = iter([_unauthorized(), _unauthorized(), httpx.Response(200, json=success)])
    h = harness(lambda request: next(answers))
    with pytest.raises(ProviderAuthFailed):
        await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 2
    await h.provider.check_create(account, creds, REFERENCE)
    assert h.events.count("token") == 3
    assert h.calls("/check/")[-1].headers["Authorization"] == f"Bearer {TOKEN_VALUE}-3"


async def test_token_cache_is_bound_to_the_environment(
    harness: Callable[..., Any], recorded: Loader, account: IssuerAccountView, creds: Any
) -> None:
    success = recorded("check_success.json")["body"]
    h = harness(
        lambda request: httpx.Response(200, json=success),
        base_urls={Environment.LIVE: "https://live.example.test"},
    )
    live = dataclasses.replace(account, environment=Environment.LIVE)
    await h.provider.check_create(account, creds, REFERENCE)
    await h.provider.check_create(live, creds, REFERENCE)
    await h.provider.check_create(account, creds, REFERENCE)
    await h.provider.check_create(live, creds, REFERENCE)
    tokens = [r.url.host for r in h.requests if r.url.path == "/v1/token"]
    assert tokens == ["einvoice-api-sandbox.sepay.vn", "live.example.test"]
    checks = h.calls("/check/")
    assert [(r.url.host, r.headers["Authorization"]) for r in checks] == [
        ("einvoice-api-sandbox.sepay.vn", f"Bearer {TOKEN_VALUE}-1"),
        ("live.example.test", f"Bearer {TOKEN_VALUE}-2"),
        ("einvoice-api-sandbox.sepay.vn", f"Bearer {TOKEN_VALUE}-1"),
        ("live.example.test", f"Bearer {TOKEN_VALUE}-2"),
    ]


@pytest.mark.parametrize(
    ("http_timeout_s", "artifact_timeout_s"), [(20.0, 90.0), (60.0, 120.0), (5.0, 5.0)]
)
async def test_timeouts_are_pinned_per_call_kind(
    http_timeout_s: float,
    artifact_timeout_s: float,
    harness: Callable[..., Any],
    recorded: Loader,
    account: IssuerAccountView,
    creds: Any,
) -> None:
    """Token, create, check and lookup use ``http_timeout_s``; downloads the artifact one."""
    success = recorded("check_success.json")["body"]
    accepted = recorded("create_accepted.json")["body"]
    lookup = recorded("get_by_reference.json")["body"]
    pdf = {"file_type": "pdf", "content": "JVBERi0xLjQ="}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=accepted)
        if request.url.path.endswith("/download"):
            kind = request.url.params["type"]
            data = pdf if kind == "pdf" else {"file_type": "xml", "content": "PEhEb24+"}
            return httpx.Response(200, json={"success": True, "data": data})
        if "/check/" in request.url.path:
            return httpx.Response(200, json=success)
        return httpx.Response(200, json=lookup)

    h = harness(handler, http_timeout_s=http_timeout_s, artifact_timeout_s=artifact_timeout_s)
    reference = lookup["data"]["reference_code"]
    await h.provider.create(account, creds, {"reference_code": reference})
    await h.provider.check_create(account, creds, reference)
    await h.provider.find_by_reference(account, creds, reference)
    await h.provider.fetch_artifacts(account, creds, reference, "unused")
    seen = [(r.url.path.rsplit("/", 1)[-1], r.extensions["timeout"]["read"]) for r in h.requests]
    assert seen == [
        ("token", http_timeout_s),
        ("create", http_timeout_s),
        (reference, http_timeout_s),
        (reference, http_timeout_s),
        ("download", artifact_timeout_s),
        ("download", artifact_timeout_s),
    ]
    assert h.client.artifact_timeout_s == artifact_timeout_s
