"""Every request is bounded by a total deadline, not only by httpx's per-read timeout: an
answer that trickles in, each piece well within the per-read timeout, is abandoned once the
deadline passes. Real sockets on 127.0.0.1 (``LocalSePay``); nothing leaves the machine."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from tests.contract.sepay_einvoice.conftest import Answer, LocalSePay, StaticResolver

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.ports.clock import SystemClock
from invoice_module.ports.provider import (
    ClientCredentials,
    IssuerAccountView,
    ProviderTransient,
)

pytestmark = pytest.mark.contract

REFERENCE = "8f3e0182-dcd9-42b5-aed2-6dd5f194f1cb"
CREATE = "/v1/invoices/create"
HTTP_TIMEOUT_S = 0.5
# Six pieces 0.2 s apart: every read waits 0.2 s, the whole answer 1.2 s.
TRICKLE = {"chunks": 6, "gap_s": 0.2}
GRANT_S = 0.3
TOKEN = {"success": True, "data": {"access_token": "synthetic-token", "expires_in": 86400}}
Loader = Callable[[str], dict[str, Any]]


def answers(path: str, answer: Answer) -> Callable[[str, str, bytes], Answer]:
    """``answer`` for ``path``, a quick token for ``/v1/token``."""

    def route(method: str, requested: str, body: bytes) -> Answer:
        if requested == "/v1/token":
            return Answer(200, TOKEN)
        assert requested == path, requested
        return answer

    return route


@asynccontextmanager
async def provider_on(
    server: LocalSePay, creds: ClientCredentials, **options: float
) -> AsyncIterator[SePayEInvoiceProvider]:
    async with server, httpx.AsyncClient(transport=server.transport()) as http:
        client = SePayEInvoiceClient(
            http,
            StaticResolver(creds),
            SystemClock(),
            ProcessRateLimiter(1000.0),
            http_timeout_s=HTTP_TIMEOUT_S,
            **options,
        )
        yield SePayEInvoiceProvider(client)


async def test_a_create_trickled_past_the_http_timeout_is_transient(
    recorded: Loader, account: IssuerAccountView, creds: ClientCredentials
) -> None:
    accepted = recorded("create_accepted.json")
    server = LocalSePay(answers(CREATE, Answer(accepted["status"], accepted["body"], **TRICKLE)))
    granted = []

    async def before_send() -> bool:
        # The grant's own database write takes part of the time the lease allows.
        granted.append(asyncio.get_running_loop().time())
        await asyncio.sleep(GRANT_S)
        return True

    async with provider_on(server, creds) as provider:
        with pytest.raises(ProviderTransient):
            await provider.create(
                account, creds, {"reference_code": REFERENCE}, before_send=before_send
            )
        ended = asyncio.get_running_loop().time()
    [post] = server.seen("POST", CREATE)
    assert json.loads(post.body) == {"reference_code": REFERENCE}
    # The deadline counts from just before the POST grant, whose lease check covers
    # ``http_timeout_s`` from then, and ends the call, not its last read.
    [grant] = granted
    assert HTTP_TIMEOUT_S - 0.05 <= ended - grant < HTTP_TIMEOUT_S + 0.15
    assert post.completed is False
    assert post.closed is not None and post.closed - grant < HTTP_TIMEOUT_S + 0.15


async def test_an_answer_within_the_http_timeout_is_read_in_full(
    recorded: Loader, account: IssuerAccountView, creds: ClientCredentials
) -> None:
    accepted = recorded("create_accepted.json")
    quick = Answer(accepted["status"], accepted["body"], chunks=3, gap_s=0.05)
    server = LocalSePay(answers(CREATE, quick))
    async with provider_on(server, creds) as provider:
        result = await provider.create(account, creds, {"reference_code": REFERENCE})
    assert result.tracking_code == REFERENCE
    assert [post.completed for post in server.seen("POST", CREATE)] == [True]


async def test_a_check_or_a_token_trickled_past_the_http_timeout_is_transient(
    recorded: Loader, account: IssuerAccountView, creds: ClientCredentials
) -> None:
    check_path = f"/v1/invoices/create/check/{REFERENCE}"
    check = recorded("check_success.json")["body"]
    server = LocalSePay(answers(check_path, Answer(200, check, **TRICKLE)))
    async with provider_on(server, creds) as provider:
        with pytest.raises(ProviderTransient):
            await provider.check_create(account, creds, REFERENCE)
    assert [get.completed for get in server.seen("GET", check_path)] == [False]

    def slow_token(method: str, path: str, body: bytes) -> Answer:
        assert path == "/v1/token", path
        return Answer(200, TOKEN, **TRICKLE)

    server = LocalSePay(slow_token)
    async with provider_on(server, creds) as provider:
        with pytest.raises(ProviderTransient):
            await provider.check_create(account, creds, REFERENCE)
    assert [token.completed for token in server.seen("POST", "/v1/token")] == [False]

    # A check refused with 401 is sent once more with a new token, under a deadline of its own.
    checks = iter([Answer(401, {"success": False}), Answer(200, check, **TRICKLE)])

    def refused_once(method: str, path: str, body: bytes) -> Answer:
        return Answer(200, TOKEN) if path == "/v1/token" else next(checks)

    server = LocalSePay(refused_once)
    async with provider_on(server, creds) as provider:
        with pytest.raises(ProviderTransient):
            await provider.check_create(account, creds, REFERENCE)
    assert [get.completed for get in server.seen("GET", check_path)] == [True, False]


@pytest.mark.parametrize(("artifact_timeout_s", "issued"), [(2.0, True), (0.8, False)])
async def test_a_download_is_bounded_by_the_artifact_timeout_in_total(
    artifact_timeout_s: float,
    issued: bool,
    account: IssuerAccountView,
    creds: ClientCredentials,
) -> None:
    pdf = b"%PDF-1.4 synthetic"
    content = {"file_type": "pdf", "file_name": "x.pdf", "content": base64.b64encode(pdf).decode()}
    xml = {"file_type": "xml", "content": base64.b64encode(b"<HDon>synthetic</HDon>").decode()}

    def route(method: str, path: str, body: bytes) -> Answer:
        if path == "/v1/token":
            return Answer(200, TOKEN)
        data = content if path.endswith("type=pdf") else xml
        return Answer(200, {"success": True, "data": data}, **TRICKLE)

    server = LocalSePay(route)
    async with provider_on(server, creds, artifact_timeout_s=artifact_timeout_s) as provider:
        if issued:
            artifacts = await provider.fetch_artifacts(account, creds, REFERENCE, "unused")
            assert artifacts.pdf == pdf
        else:
            with pytest.raises(ProviderTransient):
                await provider.fetch_artifacts(account, creds, REFERENCE, "unused")
    downloads = [e for e in server.exchanges if e.path.startswith(f"/v1/invoices/{REFERENCE}/")]
    assert [e.completed for e in downloads] == ([True, True] if issued else [False])
