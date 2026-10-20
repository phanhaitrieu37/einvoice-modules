"""Fixtures for the SePay eInvoice adapter contract tests.

Recorded sandbox responses come from ``tests/fixtures/sepay_einvoice/`` (PII already
removed); hand-built responses live in ``synthetic/`` and say so in their ``_synthetic``
field. Buyer values below are synthetic.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.domain.document import Buyer, DraftData, InvoiceDocument, freeze_document
from invoice_module.domain.enums import CrossDayAction, Environment
from invoice_module.domain.money import LineInput
from invoice_module.ports.provider import ClientCredentials, IssuerAccountView

HERE = Path(__file__).parent
RECORDED = HERE.parents[1] / "fixtures" / "sepay_einvoice"
SYNTHETIC = HERE / "synthetic"
GOLDEN = HERE / "golden"
TOKEN_VALUE = "synthetic-access-token-value"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def recorded() -> Callable[[str], dict[str, Any]]:
    return lambda name: read_json(RECORDED / name)


@pytest.fixture
def synthetic() -> Callable[[str], dict[str, Any]]:
    return lambda name: read_json(SYNTHETIC / name)


def make_account(**changes: Any) -> IssuerAccountView:
    values: dict[str, Any] = {
        "id": UUID("00000000-0000-4000-8000-0000000000a1"),
        "tenant_id": "tenant-synthetic",
        "merchant_id": UUID("00000000-0000-4000-8000-0000000000b1"),
        "environment": Environment.TEST,
        "provider_account_id": "00000000-0000-0000-0000-000000000000",
        "template_code": "1",
        "invoice_series_template": "C{YY}TSE",
        "payment_method": "TM/CK",
        "seller_store_xid": None,
        "credential_ref": "env:SYNTHETIC_SEPAY",
        "buyer_fallback_name": "Người mua không lấy hóa đơn",
        "buyer_fallback_address": "",
        "buyer_fallback_extra": None,
        "issue_date_cross_day_action": CrossDayAction.ISSUE,
    }
    values.update(changes)
    return IssuerAccountView(**values)


@pytest.fixture
def account() -> IssuerAccountView:
    return make_account()


@pytest.fixture
def creds() -> ClientCredentials:
    return ClientCredentials("synthetic-client-id", "synthetic-client-secret")


def lines() -> tuple[LineInput, ...]:
    return (
        LineInput(
            name="Phần mềm mẫu",
            unit="Tháng",
            quantity=Decimal("1"),
            unit_price_vnd=358800,
            tax_rate=10,
            item_code="SYN-001",
        ),
        LineInput(
            name="Dịch vụ cài đặt",
            unit="Lần",
            quantity=Decimal("2"),
            unit_price_vnd=50000,
            tax_rate=8,
            discount_vnd=10000,
        ),
    )


DOC_TOTAL = 358800 + 35880 + (100000 - 10000) + 7200


@pytest.fixture
def host_document(account: IssuerAccountView) -> InvoiceDocument:
    buyer = Buyer(
        name="Công ty Mẫu",
        legal_name="CÔNG TY TNHH MẪU",
        tax_code="0000000000",
        address="1 Đường Mẫu, Hà Nội",
        email="buyer@example.com",
    )
    return freeze_document(DraftData(buyer, lines(), DOC_TOTAL), account)


@pytest.fixture
def fallback_document(account: IssuerAccountView) -> InvoiceDocument:
    return freeze_document(DraftData(None, lines(), DOC_TOTAL), account)


class FakeClock:
    def __init__(self, now: datetime | None = None) -> None:
        self.value = now or datetime(2026, 10, 20, 3, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class StaticResolver:
    def __init__(self, creds: ClientCredentials) -> None:
        self.creds = creds
        self.calls = 0

    async def client_credentials(self, ref: str) -> ClientCredentials:
        self.calls += 1
        return self.creds


class RecordingLimiter(ProcessRateLimiter):
    def __init__(self, events: list[str]) -> None:
        super().__init__(1000.0)
        self.events = events

    async def acquire(self) -> None:
        self.events.append("limiter")
        await super().acquire()


Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class Harness:
    client: SePayEInvoiceClient
    provider: SePayEInvoiceProvider
    clock: FakeClock
    events: list[str]
    requests: list[httpx.Request] = field(default_factory=list)

    def calls(self, path_part: str, method: str | None = None) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if path_part in request.url.path and (method is None or request.method == method)
        ]


def token_response(value: str = TOKEN_VALUE, expires_in: int = 86400) -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": True, "data": {"access_token": value, "expires_in": expires_in}},
    )


def recorded_response(record: dict[str, Any], status: int | None = None) -> httpx.Response:
    return httpx.Response(status or record["status"], json=record["body"])


@pytest.fixture
def harness(creds: ClientCredentials) -> Callable[..., Harness]:
    def build(handler: Handler, *, token_handler: Handler | None = None) -> Harness:
        events: list[str] = []
        requests: list[httpx.Request] = []
        issued = iter(f"{TOKEN_VALUE}-{n}" for n in range(1, 100))

        def dispatch(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/v1/token":
                events.append("token")
                return token_handler(request) if token_handler else token_response(next(issued))
            events.append(f"{request.method} {request.url.path}")
            return handler(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        clock = FakeClock()
        client = SePayEInvoiceClient(http, StaticResolver(creds), clock, RecordingLimiter(events))
        return Harness(client, SePayEInvoiceProvider(client), clock, events, requests)

    return build
