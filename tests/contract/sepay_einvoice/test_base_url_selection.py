import dataclasses
from typing import Any

import httpx
import pytest

from invoice_module.adapters.sepay import SANDBOX_BASE_URL, SePayEInvoiceClient
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.domain.enums import Environment
from invoice_module.ports.clock import SystemClock
from invoice_module.ports.provider import ClientCredentials, IssuerAccountView, ProviderConfigError
from invoice_module.secrets import EnvCredentialResolver

pytestmark = pytest.mark.contract

REFERENCE = "00000000-0000-4000-8000-00000000c0de"


class Counter:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/token":
            return httpx.Response(200, json={"data": {"access_token": "t", "expires_in": 60}})
        return httpx.Response(404, json={})


def _client(counter: Counter, base_urls: Any = None) -> SePayEInvoiceClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(counter))
    return SePayEInvoiceClient(
        http, EnvCredentialResolver({}), SystemClock(), ProcessRateLimiter(1000.0), base_urls
    )


async def test_test_environment_uses_sandbox(
    account: IssuerAccountView, creds: ClientCredentials
) -> None:
    counter = Counter()
    client = _client(counter)
    assert client.base_url_for(Environment.TEST) == SANDBOX_BASE_URL
    assert SANDBOX_BASE_URL == "https://einvoice-api-sandbox.sepay.vn"
    await client.request(
        "GET", f"/v1/invoices/{REFERENCE}", account=account, creds=creds, accept=(404,)
    )
    assert {str(r.url.copy_with(path="/", query=None)) for r in counter.requests} == {
        SANDBOX_BASE_URL + "/"
    }
    explicit = _client(counter, {Environment.TEST: SANDBOX_BASE_URL})
    assert explicit.base_url_for(Environment.TEST) == SANDBOX_BASE_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://einvoice-api-sandbox.sepay.vn",
        "https://einvoice-api-sandbox.sepay.vn.example.com",
        SANDBOX_BASE_URL + "/",
    ],
)
def test_test_environment_cannot_be_overridden(url: str) -> None:
    counter = Counter()
    with pytest.raises(ProviderConfigError):
        _client(counter, {Environment.TEST: url})
    assert counter.requests == []


async def test_live_without_configured_url_raises_config_error(
    account: IssuerAccountView, creds: ClientCredentials
) -> None:
    counter = Counter()
    client = _client(counter)
    live = dataclasses.replace(account, environment=Environment.LIVE)
    with pytest.raises(ProviderConfigError):
        client.base_url_for(Environment.LIVE)
    with pytest.raises(ProviderConfigError):
        await client.request("GET", f"/v1/invoices/{REFERENCE}", account=live, creds=creds)
    assert counter.requests == []
    configured = _client(counter, {Environment.LIVE: "https://live.example.test"})
    assert configured.base_url_for(Environment.LIVE) == "https://live.example.test"
    assert configured.base_url_for(Environment.TEST) == SANDBOX_BASE_URL
