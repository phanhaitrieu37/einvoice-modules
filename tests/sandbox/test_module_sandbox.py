"""The whole module on the SePay eInvoice sandbox (never run in CI).

payment-module settles a locally signed transfer on PostgreSQL, the outbox hands it to the
invoice module, and the invoice worker issues it through the real SePay adapter. The issuer
account is registered from the accountant's configuration file; the credentials come from
``INVOICE_SANDBOX_ENV_FILE``, read in this process only. Each test records what it saw, without
ids or buyer data, under its own name in ``.evidence/p5/module-sandbox.json`` of the main
checkout; the latency test writes ``.evidence/p5/latency.json``.

Create budget per full run (the quota precheck asks for 40): register none, s1 one, the
redelivery one, the two lost responses one each, manual one, fractional one, discount one,
readiness none, latency twenty.
"""

import asyncio
import json
import logging
import os
import statistics
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import unique_prefix
from tests.integration.acceptance.conftest import TENANT, AcceptanceWorld, Order
from tests.sandbox.conftest import CREDENTIAL_REF, SANDBOX_ITEM_CODE, Sandbox, mask_ids

from invoice_module.adapters.sepay import payload
from invoice_module.adapters.sepay.endpoints import ENDPOINTS
from invoice_module.application.config import today_vn
from invoice_module.application.issuer_accounts import RegisterIssuerAccountCommand
from invoice_module.domain.enums import CrossDayAction, DeliveryMode, Environment
from invoice_module.domain.enums import IssuerAccountStatus as AccountStatus
from invoice_module.domain.money import LineInput
from invoice_module.ports.clock import SystemClock
from invoice_module.ports.provider import Accepted, ClientCredentials, IssuerAccountView
from invoice_module.runtime import InvoiceWorker, WorkerIntervals
from invoice_module.secrets import EnvCredentialResolver

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.postgres,
    pytest.mark.timeout(900),
    pytest.mark.asyncio(loop_scope="module"),
]

ISSUE_BUDGET_S = 600
QUOTA_NEEDED = 40
ACTOR = "sandbox-operator"
ACCOUNTANT = "sandbox-accountant"
WORKER = WorkerIntervals(issue_s=0.2, poll_s=0.5, sweep_s=30.0, artifacts_s=1.0)
CREATE_PATH = ENDPOINTS["create"][1]


def _evidence_dir() -> Path:
    """``INVOICE_EVIDENCE_DIR``, else ``.evidence/p5`` of the main checkout."""
    override = os.environ.get("INVOICE_EVIDENCE_DIR")
    if override:
        return Path(override).expanduser()
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(common).parent / ".evidence" / "p5"


def record_evidence(test_name: str, data: Mapping[str, Any]) -> None:
    """Merge ``data`` under ``test_name`` into ``module-sandbox.json`` (masked)."""
    path = _evidence_dir() / "module-sandbox.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    current[test_name] = mask_ids(dict(data))
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class SimulatedCrash(BaseException):
    """The worker process dies; ``BaseException`` so no ``except Exception`` swallows it."""


class LosingProvider:
    """The real adapter; for a reference in ``lose`` the create is sent and its answer read in
    full, then the worker dies before the answer is returned: the provider has the request,
    the module never learns its outcome."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.lose: set[str] = set()
        self.completed: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def create(
        self,
        account: IssuerAccountView,
        creds: ClientCredentials,
        request: Mapping[str, object],
        *,
        before_send: Any = None,
    ) -> Accepted:
        reference = str(request["reference_code"])
        accepted = await self._inner.create(account, creds, request, before_send=before_send)
        self.completed.append(reference)
        if reference in self.lose:
            self.lose.discard(reference)
            raise SimulatedCrash(f"response of the create for {reference} lost")
        return accepted


@dataclass
class ModuleSandbox:
    world: AcceptanceWorld
    provider: LosingProvider
    sandbox: Sandbox
    issuer: Any
    merchant: Any
    quota_remaining: int | None

    def __repr__(self) -> str:
        return "ModuleSandbox(<hidden>)"

    @property
    def product(self) -> Mapping[str, Any]:
        return self.sandbox.config["product_lines"][0]

    def lines(self, quantity: Decimal | None = None, discount_vnd: int = 0) -> tuple[LineInput]:
        product = self.product
        return (
            LineInput(
                name=product["name"],
                unit=product["unit"],
                quantity=Decimal(str(product["quantity"])) if quantity is None else quantity,
                unit_price_vnd=product["unit_price_vnd"],
                tax_rate=product["tax_rate"],
                discount_vnd=discount_vnd,
                item_code=SANDBOX_ITEM_CODE,
            ),
        )

    def creates(self) -> int:
        return len(self.sandbox.creates())

    def paths(self) -> list[str]:
        return [call.path for call in self.sandbox.wire]

    def assert_module_timeouts(self, since: int) -> None:
        """Every call sent since wire position ``since`` waited at most what the module's
        lease assumes: ``http_timeout_s`` per API call, ``artifact_timeout_s`` per download."""
        config = self.world.invoice.config
        calls = self.sandbox.wire[since:]
        assert calls
        for call in calls:
            seconds = (
                config.artifact_timeout_s
                if call.path.endswith("/download")
                else config.http_timeout_s
            )
            assert call.timeout == dict.fromkeys(("connect", "read", "write", "pool"), seconds), (
                call.path
            )

    async def settle(
        self, mode: DeliveryMode = DeliveryMode.AUTO, lines: tuple[LineInput, ...] | None = None
    ) -> tuple[Order, Any]:
        """A signed transfer settles a new order; returns it with its original document."""
        world = self.world
        order = await world.order(self.merchant, lines=lines or self.lines(), mode=mode)
        await world.pay(order)
        assert await world.dispatch() == 1
        return order, await world.original(order)

    async def drive_until(
        self, order: Order, done: Callable[[Any], bool], budget_s: float = ISSUE_BUDGET_S
    ) -> Any:
        """The worker's issue, poll and artifact steps once a second until ``done(document)``."""
        module = self.world.invoice
        assert module.issue_queued and module.poll_pending and module.fetch_artifacts
        deadline = time.monotonic() + budget_s
        while True:
            await module.issue_queued.run_batch()
            await module.poll_pending.run_batch()
            await module.fetch_artifacts.run_batch()
            document = await self.world.original(order)
            if done(document):
                return document
            assert time.monotonic() < deadline, f"not done in {budget_s} s: {document.status}"
            await asyncio.sleep(1)

    async def issued(self, order: Order) -> Any:
        return await self.drive_until(order, _issued_with_artifacts)

    async def expire_lease(self, document_id: UUID) -> None:
        documents = self.world.inv.documents
        async with self.world.engine.begin() as conn:
            await conn.execute(
                sa.text(
                    f"UPDATE {documents.name} SET lease_until = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": document_id},
            )

    async def lookup(self, reference: str) -> Any:
        return await self.sandbox.provider.find_by_reference(
            self.sandbox.account, self.sandbox.creds, reference
        )


def _issued_with_artifacts(document: Any) -> bool:
    return document.status == "issued" and bool(document.pdf_uri and document.xml_uri)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def module_sandbox(
    pg_url: str,
    sandbox: Sandbox,
    sandbox_env: dict[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[ModuleSandbox]:
    """Both modules on PostgreSQL, the issuer account registered from the accountant's
    configuration; fails with ``quota_below_40`` before any test when the quota is short."""
    engine = create_async_engine(pg_url)
    provider = LosingProvider(sandbox.provider)
    world = AcceptanceWorld(
        engine,
        unique_prefix(),
        tmp_path_factory.mktemp("module-sandbox"),
        provider=provider,
        clock=SystemClock(),
        credentials=EnvCredentialResolver(environ=sandbox_env),
    )
    async with engine.begin() as conn:
        await conn.run_sync(world.metadata.create_all)
    try:
        merchant = await world.merchant("sandbox-branch", "0071000000009", issuer=False)
        config = sandbox.config
        register = world.invoice.register_issuer_account
        assert register is not None
        issuer = await register.execute(
            RegisterIssuerAccountCommand(
                tenant_id=TENANT,
                merchant_id=merchant.id,
                environment=Environment.TEST,
                provider_account_id=sandbox.account.provider_account_id,
                template_code=config["template_code"],
                invoice_series_template=config["invoice_series_template"],
                payment_method=config["payment_method"],
                credential_ref=CREDENTIAL_REF,
                buyer_fallback_name=config["buyer_fallback_name"],
                buyer_fallback_address=config["buyer_fallback_address"],
                issue_date_cross_day_action=CrossDayAction(config["issue_date_cross_day_action"]),
            ),
            ACTOR,
        )
        issuer = await world.invoice.set_issuer_account_status.execute(
            issuer.id, AccountStatus.ACTIVE, ACTOR, "sandbox module run"
        )
        facts = await sandbox.provider.describe_account(sandbox.account, sandbox.creds)
        if facts.quota_remaining is not None and facts.quota_remaining < QUOTA_NEEDED:
            pytest.fail("quota_below_40")
        yield ModuleSandbox(world, provider, sandbox, issuer, merchant, facts.quota_remaining)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(world.metadata.drop_all)
        await engine.dispose()


async def test_register_sandbox_issuer_account(module_sandbox: ModuleSandbox) -> None:
    issuer, config = module_sandbox.issuer, module_sandbox.sandbox.config
    assert (issuer.environment, issuer.status) == (Environment.TEST, AccountStatus.ACTIVE)
    assert issuer.provider_account_id == module_sandbox.sandbox.account.provider_account_id
    assert issuer.credential_ref == CREDENTIAL_REF
    for key in (
        "template_code",
        "invoice_series_template",
        "payment_method",
        "buyer_fallback_name",
        "buyer_fallback_address",
    ):
        assert getattr(issuer, key) == config[key], key
    assert issuer.issue_date_cross_day_action == CrossDayAction(
        config["issue_date_cross_day_action"]
    )
    quota = module_sandbox.quota_remaining
    record_evidence(
        "test_register_sandbox_issuer_account",
        {"registered": True, "quota_remaining": quota, "quota_needed": QUOTA_NEEDED},
    )
    assert quota is None or quota >= QUOTA_NEEDED, "quota_below_40"


async def test_module_s1_settled_to_issued_sandbox(module_sandbox: ModuleSandbox) -> None:
    sandbox = module_sandbox.sandbox
    creates_before = module_sandbox.creates()
    started = time.monotonic()
    order, _ = await module_sandbox.settle()
    document = await module_sandbox.issued(order)
    minutes = (time.monotonic() - started) / 60

    check = await sandbox.raw_get(
        ENDPOINTS["check_create"][1].replace("{tracking_code}", document.tracking_code)
    )
    invoice = check["data"]["invoice"]
    progress = payload.parse_progress(check)
    config = module_sandbox.world.invoice.config
    fetch_started = time.monotonic()
    artifacts = await sandbox.provider.fetch_artifacts(
        sandbox.account, sandbox.creds, document.reference_code, document.provider_invoice_id
    )
    fetch_seconds = time.monotonic() - fetch_started
    record_evidence(
        "test_module_s1_settled_to_issued_sandbox",
        {
            "status": document.status,
            "minutes_to_issued": round(minutes, 2),
            "total_equals_settled": document.total_vnd == document.settled_amount_vnd,
            "request_total_equals_settled": document.request["total_amount"]
            == document.settled_amount_vnd,
            # Both values are kept: the sandbox may fill the code in after the issue.
            "tax_authority_code_stored": document.tax_authority_code,
            "tax_authority_code_at_check": progress.tax_authority_code,
            "tracking_equals_reference": document.tracking_code == document.reference_code,
            "check_document_type": invoice.get("document_type"),
            "check_invoice_status": invoice.get("status"),
            # SePay returns no invoice id, so the artifacts came by reference.
            "provider_invoice_id_is_null": document.provider_invoice_id is None,
            "artifact_fetch_seconds": round(fetch_seconds, 2),
            "artifact_timeout_s": config.artifact_timeout_s,
            "creates": module_sandbox.creates() - creates_before,
        },
    )
    assert document.invoice_number
    # The stored code is the one the check answered, or empty when the sandbox filled it later.
    assert document.tax_authority_code in (None, progress.tax_authority_code)
    assert document.total_vnd == document.settled_amount_vnd == order.amount_vnd
    assert document.request["total_amount"] == document.settled_amount_vnd
    assert document.tracking_code == document.reference_code
    assert module_sandbox.creates() - creates_before == 1
    assert minutes <= 10
    assert invoice.get("status") == "issued"
    assert artifacts.pdf.startswith(b"%PDF") and artifacts.xml
    assert fetch_seconds < config.artifact_timeout_s


async def test_module_redelivery_100_times_one_provider_invoice(
    module_sandbox: ModuleSandbox,
) -> None:
    world = module_sandbox.world
    creates_before = module_sandbox.creates()
    wire_before = len(module_sandbox.sandbox.wire)
    delivered_before = len(world.delivered.events)
    order, document = await module_sandbox.settle()
    event = world.delivered.events[delivered_before]
    stop = asyncio.Event()
    workers = [InvoiceWorker(world.build_invoice(), WORKER) for _ in range(3)]
    running = [asyncio.create_task(worker.run(stop)) for worker in workers]

    async def redeliver(times: int) -> None:
        for _ in range(times):
            await world.publisher.publish(event)

    try:
        await asyncio.gather(*(redeliver(25) for _ in range(4)))
        async with asyncio.timeout(ISSUE_BUDGET_S):
            while not _issued_with_artifacts(document := await world.original(order)):
                await asyncio.sleep(1)
    finally:
        stop.set()
        await asyncio.gather(*running)

    documents = world.inv.documents
    originals = await world.scalar(
        sa.select(sa.func.count())
        .select_from(documents)
        .where(documents.c.settlement_id == document.settlement_id, documents.c.kind == "original")
    )
    found = await module_sandbox.lookup(document.reference_code)
    record_evidence(
        "test_module_redelivery_100_times_one_provider_invoice",
        {
            "deliveries": len(world.delivered.events) - delivered_before,
            "originals": originals,
            "creates": module_sandbox.creates() - creates_before,
            "lookup_state": found.state if found else None,
            "lookup_number_matches": bool(
                found and found.invoice_number == document.invoice_number
            ),
        },
    )
    assert len(world.delivered.events) - delivered_before == 101
    assert originals == 1
    assert module_sandbox.creates() - creates_before == 1
    assert found is not None and found.state == "success"
    assert found.invoice_number == document.invoice_number
    module_sandbox.assert_module_timeouts(wire_before)


async def _lost_response_recovers(
    module_sandbox: ModuleSandbox,
    order: Order,
    document: Any,
    send: Callable[[], Awaitable[Any]],
) -> tuple[Any, int, dict[str, Any]]:
    """``send`` makes the worker send the create whose answer is lost; the lease then runs out
    and the next claim looks the reference up before anything else.

    The third value records the first claim after the crash: whether it consulted the job
    check (only when the lookup did not yet see the accepted create) and what the lookup
    answered first."""
    creates_before = module_sandbox.creates()
    module_sandbox.provider.lose.add(document.reference_code)
    with pytest.raises(SimulatedCrash):
        await send()
    assert module_sandbox.provider.completed[-1] == document.reference_code
    assert module_sandbox.creates() - creates_before == 1
    crashed = await module_sandbox.world.original(order)
    assert crashed.request is not None and crashed.lease_until is not None
    await module_sandbox.expire_lease(document.id)
    paths_before = len(module_sandbox.paths())
    assert module_sandbox.world.invoice.issue_queued is not None
    await module_sandbox.world.invoice.issue_queued.run_batch()
    first_calls = module_sandbox.sandbox.wire[paths_before:]
    first_after = [call.path for call in first_calls]
    lookup_path = ENDPOINTS["get_by_reference"][1].replace(
        "{reference_code}", document.reference_code
    )
    check_path = ENDPOINTS["check_create"][1].replace("{tracking_code}", document.reference_code)
    assert lookup_path in first_after and CREATE_PATH not in first_after
    first_lookup = next(call for call in first_calls if call.path == lookup_path)
    first_claim = {
        "check_consulted": check_path in first_after,
        "first_lookup_status": first_lookup.status,
        "first_lookup_error_code": first_lookup.error_code,
    }
    after_claim = await module_sandbox.world.original(order)
    first_claim["status_after_first_claim"] = after_claim.status
    assert after_claim.status in ("pending_provider", "issued"), after_claim.status
    issued = await module_sandbox.issued(order)
    return issued, module_sandbox.creates() - creates_before, first_claim


async def test_module_lost_response_recovers_via_lookup_sandbox(
    module_sandbox: ModuleSandbox,
) -> None:
    config = module_sandbox.world.invoice.config
    assert (config.lease_seconds, config.http_timeout_s) == (60, 20.0)
    wire_before = len(module_sandbox.sandbox.wire)
    order, document = await module_sandbox.settle()
    issue = module_sandbox.world.invoice.issue_queued
    assert issue is not None
    issued, creates, first_claim = await _lost_response_recovers(
        module_sandbox, order, document, issue.run_batch
    )
    found = await module_sandbox.lookup(issued.reference_code)
    record_evidence(
        "test_module_lost_response_recovers_via_lookup_sandbox",
        {
            "status": issued.status,
            "creates": creates,
            **first_claim,
            "lookup_state": found.state if found else None,
            "lookup_number_matches": bool(found and found.invoice_number == issued.invoice_number),
        },
    )
    assert issued.status == "issued"
    assert creates == 1
    assert found is not None and found.invoice_number == issued.invoice_number
    module_sandbox.assert_module_timeouts(wire_before)


async def test_module_manual_create_lost_response_recovers_sandbox(
    module_sandbox: ModuleSandbox,
) -> None:
    order, document = await module_sandbox.settle(DeliveryMode.MANUAL)
    document = await module_sandbox.drive_until(
        order, lambda doc: doc.status == "awaiting_approval", budget_s=60
    )
    await module_sandbox.world.invoice.approve_draft_invoice.execute(document.id, ACCOUNTANT)
    issue = module_sandbox.world.invoice.issue_queued
    assert issue is not None
    issued, creates, first_claim = await _lost_response_recovers(
        module_sandbox, order, document, issue.run_batch
    )
    found = await module_sandbox.lookup(issued.reference_code)
    record_evidence(
        "test_module_manual_create_lost_response_recovers_sandbox",
        {
            "status": issued.status,
            "creates": creates,
            **first_claim,
            "issued_date_is_provider_date": bool(found and issued.issued_date == found.issued_date),
            "approved_by_recorded": issued.approved_by == ACCOUNTANT,
        },
    )
    assert issued.status == "issued"
    assert creates == 1
    assert found is not None and issued.issued_date == found.issued_date
    assert issued.approved_by == ACCOUNTANT


async def test_module_manual_approve_then_create_sandbox(module_sandbox: ModuleSandbox) -> None:
    order, document = await module_sandbox.settle(DeliveryMode.MANUAL)
    paths_before = len(module_sandbox.paths())
    creates_before = module_sandbox.creates()
    document = await module_sandbox.drive_until(
        order, lambda doc: doc.status == "awaiting_approval", budget_s=60
    )
    assert module_sandbox.paths()[paths_before:] == []  # no SePay call before the approval
    assert await module_sandbox.lookup(document.reference_code) is None

    approved_at = len(module_sandbox.paths())
    await module_sandbox.world.invoice.approve_draft_invoice.execute(document.id, ACCOUNTANT)
    sent_on: date = today_vn(SystemClock().now())
    issued = await module_sandbox.issued(order)
    worker_paths = module_sandbox.paths()[approved_at:]
    found = await module_sandbox.lookup(issued.reference_code)
    paths = module_sandbox.paths()[paths_before:]
    issue_calls = [path for path in paths if path.startswith("/v1/invoices/issue")]
    lookup_path = ENDPOINTS["get_by_reference"][1].replace(
        "{reference_code}", issued.reference_code
    )
    lookup_first = (
        lookup_path in worker_paths
        and CREATE_PATH in worker_paths
        and worker_paths.index(lookup_path) < worker_paths.index(CREATE_PATH)
    )
    record_evidence(
        "test_module_manual_approve_then_create_sandbox",
        {
            "status": issued.status,
            "creates": module_sandbox.creates() - creates_before,
            "issue_endpoint_calls": len(issue_calls),
            "issued_date_equals_sent": issued.issued_date == sent_on,
            "issued_date_equals_lookup": bool(found and issued.issued_date == found.issued_date),
            "approved_at_present": issued.approved_at is not None,
            "approved_by_recorded": issued.approved_by == ACCOUNTANT,
            "lookup_before_create": lookup_first,
        },
    )
    assert issued.status == "issued" and issued.invoice_number
    assert module_sandbox.creates() - creates_before == 1
    assert worker_paths.count(CREATE_PATH) == 1
    assert lookup_first, worker_paths
    assert issued.request["is_draft"] is False
    assert issued.issued_date == sent_on
    assert found is not None and found.state == "success"
    assert found.issued_date == issued.issued_date
    assert issued.approved_at is not None and issued.approved_by == ACCOUNTANT
    assert issue_calls == []


async def test_module_fractional_quantity_sandbox(module_sandbox: ModuleSandbox) -> None:
    sandbox = module_sandbox.sandbox
    lines = module_sandbox.lines(quantity=Decimal("1.5"))
    order, _ = await module_sandbox.settle(lines=lines)
    issued = await module_sandbox.issued(order)
    body = await sandbox.raw_get(
        ENDPOINTS["get_by_reference"][1].replace("{reference_code}", issued.reference_code)
    )
    data = body["data"]
    items = data.get("items") or []
    record_evidence(
        "test_module_fractional_quantity_sandbox",
        {
            "status": issued.status,
            "quantity_sent": issued.request["items"][0]["quantity"],
            "lookup_quantities": [str(item.get("quantity")) for item in items],
            "lookup_total_equals_settled": Decimal(str(data.get("total_amount")))
            == issued.settled_amount_vnd,
        },
    )
    assert issued.status == "issued"
    assert issued.request["items"][0]["quantity"] == "1.5"
    assert [Decimal(str(item["quantity"])) for item in items] == [Decimal("1.50")]
    assert Decimal(str(data["total_amount"])) == issued.settled_amount_vnd == order.amount_vnd


async def test_module_discount_line_total_matches_lookup_sandbox(
    module_sandbox: ModuleSandbox,
) -> None:
    discount = module_sandbox.product["unit_price_vnd"] // 10
    assert discount > 0
    order, _ = await module_sandbox.settle(lines=module_sandbox.lines(discount_vnd=discount))
    issued = await module_sandbox.issued(order)
    found = await module_sandbox.lookup(issued.reference_code)
    record_evidence(
        "test_module_discount_line_total_matches_lookup_sandbox",
        {
            "status": issued.status,
            "discount_sent": issued.request["items"][0].get("discount_amount"),
            "lookup_total_equals_document": bool(
                found and found.total_amount_vnd == issued.total_vnd
            ),
        },
    )
    assert issued.status == "issued"
    assert issued.total_vnd == issued.settled_amount_vnd == order.amount_vnd
    assert found is not None and found.total_amount_vnd == issued.total_vnd


def _readiness_evidence() -> Path:
    """``INVOICE_READINESS_EVIDENCE``, else the newest probe evidence of the main checkout."""
    override = os.environ.get("INVOICE_READINESS_EVIDENCE")
    if override:
        return Path(override).expanduser()
    found = sorted((_evidence_dir().parent / "sandbox").glob("*.json"))
    assert found, "no probe evidence file: run tools.einvoice_probe evidence first"
    return found[-1]


async def test_record_issuer_readiness_sandbox(module_sandbox: ModuleSandbox) -> None:
    """The probe's evidence file is recorded on the sandbox account; no create."""
    evidence = _readiness_evidence()
    assert evidence.is_file(), "readiness evidence file missing"
    world = module_sandbox.world
    readiness = world.invoice.record_issuer_readiness
    assert readiness is not None
    creates_before = module_sandbox.creates()
    result = await readiness.execute(module_sandbox.issuer.id, str(evidence), ACTOR)
    accounts = world.inv.issuer_accounts
    stored = await world.scalar(
        sa.select(accounts.c.sandbox_evidence_ref).where(accounts.c.id == module_sandbox.issuer.id)
    )
    record_evidence(
        "test_record_issuer_readiness_sandbox",
        {
            "evidence_file": evidence.name,
            "account_matches": result.account_id == module_sandbox.issuer.id,
            "warnings": list(result.warnings),
            "evidence_ref_stored": stored == str(evidence),
            "creates": module_sandbox.creates() - creates_before,
        },
    )
    assert result.account_id == module_sandbox.issuer.id
    assert result.warnings == ()
    assert stored == str(evidence)
    assert module_sandbox.creates() - creates_before == 0


def _call_kind(path: str) -> str:
    if path == CREATE_PATH:
        return "create"
    if path.startswith(CREATE_PATH + "/check/"):
        return "check"
    if "/download" in path:
        return "download"
    if path.startswith("/v1/invoices/"):
        return "lookup"
    return "other"


class _ClientLog(logging.Handler):
    """Counts the SePay client's warning lines (abandoned requests and non-OK answers) without
    keeping any of their text."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.deadlines: list[str] = []
        self.failures: list[str] = []
        self.answers: list[tuple[str, str, int]] = []

    def emit(self, record: logging.LogRecord) -> None:
        args = record.args if isinstance(record.args, tuple) else ()
        message = record.msg if isinstance(record.msg, str) else ""
        if message.endswith("total deadline passed") and args:
            self.deadlines.append(str(args[0]))
        elif message == "sepay einvoice %s request failed: %s" and len(args) == 2:
            self.failures.append(f"{args[0]} {args[1]}")
        elif message.startswith("sepay einvoice %s %s -> HTTP") and len(args) >= 3:
            kind = _call_kind(str(args[1]).split("?")[0])
            self.answers.append((str(args[0]), kind, int(args[2])))

    def marks(self) -> tuple[int, int, int]:
        return len(self.deadlines), len(self.failures), len(self.answers)

    def summary(self, since: tuple[int, int, int] = (0, 0, 0)) -> dict[str, Any]:
        deadlines = self.deadlines[since[0] :]
        failures = self.failures[since[1] :]
        answers = self.answers[since[2] :]
        return {
            "deadline_passed": {m: deadlines.count(m) for m in sorted(set(deadlines))},
            "request_failed": {f: failures.count(f) for f in sorted(set(failures))},
            "non_ok_answers": {
                f"{method} {kind} {status}": answers.count((method, kind, status))
                for method, kind, status in sorted(set(answers))
            },
        }


@pytest.mark.timeout(1800)
async def test_latency_p95_twenty_settlements(module_sandbox: ModuleSandbox) -> None:
    """Twenty settlements, one after the other, with the worker running as a host runs it.

    Besides the percentiles, ``latency.json`` records the provider's health during the run
    (abandoned requests and non-OK answers), each settlement's send and poll counters, and
    one lookup per reference after the run."""
    world = module_sandbox.world
    config = world.invoice.config
    client_log = _ClientLog()
    client_logger = logging.getLogger("invoice_module.adapters.sepay.client")
    client_logger.addHandler(client_log)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    stop = asyncio.Event()
    running = asyncio.create_task(InvoiceWorker(world.build_invoice(), WORKER).run(stop))
    seconds: list[float] = []
    documents: list[Any] = []
    per_settlement: list[dict[str, Any]] = []
    try:
        for _ in range(20):
            since = client_log.marks()
            wire_from = len(module_sandbox.sandbox.wire)
            order = await world.order(module_sandbox.merchant, lines=module_sandbox.lines())
            await world.pay(order)
            assert await world.dispatch() == 1
            async with asyncio.timeout(ISSUE_BUDGET_S):
                while (document := await world.original(order)).status != "issued":
                    await asyncio.sleep(0.5)
            seconds.append((document.issued_at - document.settled_at).total_seconds())
            documents.append(document)
            mine = [
                _call_kind(call.path.split("?")[0])
                for call in module_sandbox.sandbox.wire[wire_from:]
                if document.reference_code in call.path or call.path == CREATE_PATH
            ]
            per_settlement.append(
                {
                    "seconds": round(seconds[-1], 2),
                    "post_count": document.post_count,
                    "attempts": document.attempts,
                    "poll_count": document.poll_count,
                    "answered_calls": mine,
                    **client_log.summary(since),
                }
            )
    finally:
        stop.set()
        await running
        client_logger.removeHandler(client_log)
    finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    for document, row in zip(documents, per_settlement, strict=True):
        found = await module_sandbox.lookup(document.reference_code)
        row["provider_state"] = found.state if found else None
        row["lookup_number_matches"] = bool(
            found and found.invoice_number == document.invoice_number
        )
    ordered = sorted(seconds)
    latency = {
        "n": len(ordered),
        "p50_s": round(statistics.median(ordered), 2),
        "p95_s": round(ordered[max(0, -(-95 * len(ordered) // 100) - 1)], 2),
        "max_s": round(ordered[-1], 2),
    }
    record = {
        **latency,
        "run": {
            "started_at": started_at,
            "finished_at": finished_at,
            "http_timeout_s": config.http_timeout_s,
            "lease_seconds": config.lease_seconds,
            "poll_schedule_s": list(config.poll_schedule_s),
            "worker_issue_s": WORKER.issue_s,
            "worker_poll_s": WORKER.poll_s,
        },
        "provider_health": client_log.summary(),
        "settlements": per_settlement,
    }
    path = _evidence_dir() / "latency.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mask_ids(record), indent=2) + "\n", encoding="utf-8")
    assert latency["n"] == 20
    assert latency["p95_s"] <= 60, latency
