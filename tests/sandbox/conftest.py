"""Fixtures for adapter tests against the SePay eInvoice sandbox.

Credentials are read from the env file named by ``INVOICE_SANDBOX_ENV_FILE`` inside this
process only: the values go to ``EnvCredentialResolver(environ=...)``, never to
``os.environ``, the terminal or the evidence files. Evidence summaries (no ids, no buyer
data) are written under ``.evidence/sandbox-adapter/`` at the repository root.
"""

import asyncio
import json
import os
import re
import subprocess
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.adapters.sepay.redact import redact
from invoice_module.application.config import InvoiceModuleConfig
from invoice_module.domain.document import Buyer, DraftData, InvoiceDocument, freeze_document
from invoice_module.domain.enums import CrossDayAction, Environment
from invoice_module.domain.money import LineInput, compute_total
from invoice_module.ports.clock import SystemClock
from invoice_module.ports.provider import (
    ClientCredentials,
    IssuerAccountView,
    ProviderProgress,
)
from invoice_module.secrets import EnvCredentialResolver
from tools.einvoice_probe.envfile import load_env_file

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
EVIDENCE = ROOT / ".evidence" / "sandbox-adapter"
FINDINGS = ROOT / "tests" / "fixtures" / "sepay_einvoice" / "findings.json"
CREDENTIAL_REF = "env:SEPAY_EINVOICE_SANDBOX"
VN_TZ = ZoneInfo("Asia/Saigon")
POLL_DELAYS = (3, 5, 10, 20, 30, 60)
POLL_BUDGET_S = 600
# The module's own timeouts: a create may not outlive the lease extended before it is sent
# (``http_timeout_s``); downloads have their own, longer ``artifact_timeout_s``.
MODULE_CONFIG = InvoiceModuleConfig()
# The sandbox refuses a goods line without item_code; the host supplies one.
SANDBOX_ITEM_CODE = "SANDBOX-PHOTOSHOP"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    wrong = [
        item.nodeid
        for item in items
        if HERE in item.path.parents
        and (
            item.get_closest_marker("sandbox") is None
            or item.get_closest_marker("integration") is not None
        )
    ]
    if wrong:
        raise pytest.UsageError(
            f"sandbox tests need the sandbox marker and no integration: {wrong}"
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Render sandbox failures without argument values, so no credential reaches the log."""
    report = yield
    if HERE in item.path.parents and report.failed and call.excinfo is not None:
        report.longrepr = call.excinfo.getrepr(style="short", funcargs=False, showlocals=False)
    return report


def today_vn() -> date:
    return datetime.now(VN_TZ).date()


def mask_ids(value: Any) -> Any:
    """Evidence copy: buyer data redacted, UUIDs and invoice numbers replaced."""
    text = json.dumps(redact(value), ensure_ascii=False, default=str)
    text = _UUID.sub("<uuid>", text)
    return json.loads(text)


def write_evidence(name: str, data: dict[str, Any]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamped = {"recorded_at": datetime.now(VN_TZ).isoformat(timespec="seconds"), **data}
    (EVIDENCE / f"{name}.json").write_text(
        json.dumps(mask_ids(stamped), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _accounting_config_path() -> Path:
    override = os.environ.get("INVOICE_ACCOUNTING_CONFIG")
    if override:
        return Path(override).expanduser()
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(common).parent / "plans" / "reports" / "accounting-config.json"


@dataclass
class WireCall:
    method: str
    path: str
    status: int
    error_code: str | None = None
    error_message: str | None = None
    content_type: str | None = None
    # Decoded JSON body of API calls other than the token request and downloads (kept in
    # memory only; evidence copies go through ``mask_ids``).
    body: Any = None
    # The timeout the request was sent with (httpx's per-phase seconds).
    timeout: dict[str, float] | None = None


@dataclass(repr=False)
class Sandbox:
    """Never shown in assertion output: it holds the resolved credentials."""

    provider: SePayEInvoiceProvider
    client: SePayEInvoiceClient
    creds: ClientCredentials
    account: IssuerAccountView
    config: dict[str, Any]
    wire: list[WireCall] = field(default_factory=list)

    def __repr__(self) -> str:
        return "Sandbox(<hidden>)"

    def creates(self) -> list[WireCall]:
        return [c for c in self.wire if c.method == "POST" and c.path == "/v1/invoices/create"]

    def document(self, buyer: Buyer | None, quantity: Decimal | None = None) -> InvoiceDocument:
        """The e2e product line; ``quantity`` replaces the configured one when given."""
        product = self.config["product_lines"][0]
        line = LineInput(
            name=product["name"],
            unit=product["unit"],
            quantity=Decimal(str(product["quantity"])) if quantity is None else quantity,
            unit_price_vnd=product["unit_price_vnd"],
            tax_rate=product["tax_rate"],
            item_code=SANDBOX_ITEM_CODE,
        )
        return freeze_document(DraftData(buyer, (line,), compute_total((line,))), self.account)

    @staticmethod
    def today() -> date:
        return today_vn()

    @staticmethod
    def evidence(name: str, data: dict[str, Any]) -> None:
        write_evidence(name, data)

    async def poll(self, tracking_code: str) -> tuple[ProviderProgress, int]:
        """Poll ``check_create`` on the 3, 5, 10, 20, 30, 60 s schedule for up to 10 min."""
        waited = 0
        polls = 0
        delays = iter(POLL_DELAYS)
        while True:
            delay = next(delays, POLL_DELAYS[-1])
            await asyncio.sleep(delay)
            waited += delay
            polls += 1
            progress = await self.provider.check_create(self.account, self.creds, tracking_code)
            if progress.state != "pending" or waited >= POLL_BUDGET_S:
                return progress, polls

    async def raw_get(self, path: str) -> Any:
        response = await self.client.request("GET", path, account=self.account, creds=self.creds)
        return response.body


@pytest.fixture(scope="session")
def sandbox_env() -> dict[str, str]:
    path = os.environ.get("INVOICE_SANDBOX_ENV_FILE")
    if not path:
        pytest.fail("INVOICE_SANDBOX_ENV_FILE is not set")
    env = load_env_file(Path(path).expanduser())
    if env.get("SEPAY_EINVOICE_ENVIRONMENT") != "test":
        pytest.fail("the sandbox env file must declare SEPAY_EINVOICE_ENVIRONMENT=test")
    return env


@pytest.fixture(scope="session")
def accounting_config() -> dict[str, Any]:
    config = json.loads(_accounting_config_path().read_text(encoding="utf-8"))
    assert len(config["product_lines"]) >= 1
    return config


@pytest.fixture(scope="session")
def findings() -> dict[str, Any]:
    return json.loads(FINDINGS.read_text(encoding="utf-8"))


def _bootstrap_account(config: dict[str, Any], provider_account_id: str) -> IssuerAccountView:
    return IssuerAccountView(
        id=UUID("00000000-0000-4000-8000-00000000a0a0"),
        tenant_id="sandbox",
        merchant_id=UUID("00000000-0000-4000-8000-00000000b0b0"),
        environment=Environment.TEST,
        provider_account_id=provider_account_id,
        template_code=config["template_code"],
        invoice_series_template=config["invoice_series_template"],
        payment_method=config["payment_method"],
        seller_store_xid=None,
        credential_ref=CREDENTIAL_REF,
        buyer_fallback_name=config["buyer_fallback_name"],
        buyer_fallback_address=config["buyer_fallback_address"],
        buyer_fallback_extra=None,
        issue_date_cross_day_action=CrossDayAction(config["issue_date_cross_day_action"]),
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def sandbox(
    sandbox_env: dict[str, str], accounting_config: dict[str, Any]
) -> AsyncIterator[Sandbox]:
    wire: list[WireCall] = []

    async def record(response: httpx.Response) -> None:
        await response.aread()
        path = response.request.url.path
        call = WireCall(response.request.method, path, response.status_code)
        call.content_type = response.headers.get("content-type")
        call.timeout = dict(response.request.extensions["timeout"])
        if path != "/v1/token" and not path.endswith("/download"):
            try:
                call.body = json.loads(response.content)
            except ValueError:
                call.body = None
        if response.status_code >= 400 and path != "/v1/token":
            try:
                error = json.loads(response.content).get("error") or {}
                call.error_code, call.error_message = error.get("code"), error.get("message")
            except (ValueError, AttributeError):
                pass
        wire.append(call)

    resolver = EnvCredentialResolver(environ=sandbox_env)
    creds = await resolver.client_credentials(CREDENTIAL_REF)
    async with httpx.AsyncClient(event_hooks={"response": [record]}) as http:
        client = SePayEInvoiceClient(
            http,
            resolver,
            SystemClock(),
            ProcessRateLimiter(2.0),
            http_timeout_s=MODULE_CONFIG.http_timeout_s,
            artifact_timeout_s=MODULE_CONFIG.artifact_timeout_s,
        )
        provider = SePayEInvoiceProvider(client)
        bootstrap = _bootstrap_account(accounting_config, "")
        ids = await provider.provider_account_ids(bootstrap, creds)
        series = bootstrap.invoice_series_template.replace("{YY}", f"{today_vn().year % 100:02d}")
        account = None
        for provider_account_id in ids:
            candidate = _bootstrap_account(accounting_config, provider_account_id)
            facts = await provider.describe_account(candidate, creds)
            if (candidate.template_code, series) in zip(facts.templates, facts.series, strict=True):
                account = candidate
                break
        assert account is not None, "no sandbox provider account offers the configured template"
        yield Sandbox(provider, client, creds, account, accounting_config, wire)
