"""Fixtures for the SePay eInvoice adapter contract tests.

Recorded sandbox responses come from ``tests/fixtures/sepay_einvoice/`` (PII already
removed); hand-built responses live in ``synthetic/`` and say so in their ``_synthetic``
field. Buyer values below are synthetic.
"""

import asyncio
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


def timeout_of(request: httpx.Request) -> dict[str, float]:
    return dict(request.extensions["timeout"])


def all_phases(seconds: float) -> dict[str, float]:
    return {"connect": seconds, "read": seconds, "write": seconds, "pool": seconds}


@pytest.fixture
def harness(creds: ClientCredentials) -> Callable[..., Harness]:
    """A client over ``MockTransport``; every request must carry the pinned timeout.

    Downloads use ``artifact_timeout_s``, every other call (token included)
    ``http_timeout_s``; both default to the client defaults (20 s and 90 s).
    """

    def build(
        handler: Handler,
        *,
        token_handler: Handler | None = None,
        http_timeout_s: float = 20.0,
        artifact_timeout_s: float = 90.0,
        **client_options: Any,
    ) -> Harness:
        events: list[str] = []
        requests: list[httpx.Request] = []
        issued = iter(f"{TOKEN_VALUE}-{n}" for n in range(1, 100))

        def dispatch(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            download = request.url.path.endswith("/download")
            expected = artifact_timeout_s if download else http_timeout_s
            assert timeout_of(request) == all_phases(expected), request.url.path
            if request.url.path == "/v1/token":
                events.append("token")
                return token_handler(request) if token_handler else token_response(next(issued))
            events.append(f"{request.method} {request.url.path}")
            return handler(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        clock = FakeClock()
        options: dict[str, Any] = {**client_options}
        if http_timeout_s != 20.0:
            options["http_timeout_s"] = http_timeout_s
        if artifact_timeout_s != 90.0:
            options["artifact_timeout_s"] = artifact_timeout_s
        client = SePayEInvoiceClient(
            http, StaticResolver(creds), clock, RecordingLimiter(events), **options
        )
        return Harness(client, SePayEInvoiceProvider(client), clock, events, requests)

    return build


@dataclass(frozen=True)
class Answer:
    """An HTTP answer of ``LocalSePay``: the head goes out at once, then the body in ``chunks``
    pieces, each sent ``gap_s`` after the previous one."""

    status: int
    body: Any
    chunks: int = 1
    gap_s: float = 0.0

    def encoded(self) -> bytes:
        return json.dumps(self.body).encode()


@dataclass
class Exchange:
    """One request seen by ``LocalSePay`` and how long its connection stayed open (loop time)."""

    method: str
    path: str
    body: bytes
    opened: float
    closed: float | None = None
    # False when the client left before the whole answer was sent.
    completed: bool = False


class LocalSePay:
    """A real-socket HTTP/1.1 stand-in for the sandbox host on 127.0.0.1, for answers that
    trickle in. ``transport()`` sends every request of a client to it whatever the URL's
    host, so the client keeps its sandbox base URL; one request per connection."""

    def __init__(self, answer: Callable[[str, str, bytes], Answer]) -> None:
        self._answer = answer
        self._server: asyncio.Server | None = None
        self.exchanges: list[Exchange] = []

    async def __aenter__(self) -> "LocalSePay":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def transport(self) -> httpx.AsyncBaseTransport:
        assert self._server is not None
        return _ToLocal(self._server.sockets[0].getsockname()[1])

    def seen(self, method: str, path: str) -> list[Exchange]:
        return [e for e in self.exchanges if (e.method, e.path) == (method, path)]

    @staticmethod
    def most_open_at_once(exchanges: list[Exchange]) -> int:
        """The largest number of these exchanges whose connections were open together."""
        edges = sorted(
            [(e.opened, 1) for e in exchanges]
            + [(e.closed if e.closed is not None else float("inf"), -1) for e in exchanges]
        )
        open_now = most = 0
        for _, step in edges:
            open_now += step
            most = max(most, open_now)
        return most

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        exchange: Exchange | None = None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            method, path, _ = head.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            body = await reader.readexactly(length) if length else b""
            exchange = Exchange(method, path, body, loop.time())
            self.exchanges.append(exchange)
            answer = self._answer(method, path, body)
            content = answer.encoded()
            writer.write(
                f"HTTP/1.1 {answer.status} X\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            step = -(-len(content) // answer.chunks)
            for start in range(0, len(content), step):
                if answer.gap_s and await _left(reader, answer.gap_s):
                    break
                writer.write(content[start : start + step])
                await writer.drain()
            else:
                exchange.completed = True
            exchange.closed = loop.time()
        except (ConnectionError, asyncio.IncompleteReadError):
            if exchange is not None:
                exchange.closed = loop.time()
        finally:
            writer.close()


async def _left(reader: asyncio.StreamReader, seconds: float) -> bool:
    """Whether the client closed its connection within ``seconds``; it sends nothing more
    after its request, so a read only ends at the connection's end."""
    try:
        async with asyncio.timeout(seconds):
            await reader.read(1)
    except TimeoutError:
        return False
    return True


class _ToLocal(httpx.AsyncHTTPTransport):
    def __init__(self, port: int) -> None:
        super().__init__()
        self._port = port

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port)
        return await super().handle_async_request(request)
