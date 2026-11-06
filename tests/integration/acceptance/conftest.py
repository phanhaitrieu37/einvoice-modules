"""Acceptance world on real PostgreSQL: both modules on one database, no network.

An order is a payment-module intent with (or without) an invoice draft. Paying it posts a
transfer signed with the webhook secret to the payment-module webhook router; the inbox settles
it, the outbox fans ``PaymentSettled`` out through ``FanOutPublisher`` to the invoice consumer,
and the invoice worker steps issue it at ``FakeEInvoiceProvider``. The invoice side runs on a
``FakeClock`` (started at the real time) so leases, polls and the overdue sweep can be moved on;
the payment side keeps the system clock, which checks the webhook timestamp.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from examples.invoice_host.server import create_app
from examples.invoice_host.sign_webhook import signed_delivery
from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.adapters.sqlalchemy.tables import define_tables as define_payment_tables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.create_intent import CreateIntentCommand
from payment_module.builder import build_payment_module
from payment_module.domain.enums import ConnectionStatus
from payment_module.domain.enums import Environment as PaymentEnvironment
from payment_module.ports.clock import SystemClock
from payment_module.ports.publisher import OutboxEventView
from payment_module.secrets import EnvSecretResolver
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.conftest import unique_prefix
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider
from tests.fakes.recording import FakeClock, RecordingMetrics, StaticCredentials

from invoice_module.adapters.sqlalchemy.tables import define_tables as define_invoice_tables
from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWorkFactory
from invoice_module.adapters.storage import FileArtifactStore
from invoice_module.application.config import today_vn
from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.builder import InvoiceModule, build_invoice_module
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.money import LineInput, compute_total
from invoice_module.integrations.payment_module import FanOutPublisher, PaymentSettledConsumer
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.provider import EInvoiceProvider

TENANT, ADMIN, ORDER = "tenant-acceptance", "admin-acceptance", "order"
SECRET_REF = "env:ACCEPTANCE_WEBHOOK_SECRET"


def lines_for(amount: int, tax_rate: int = 0) -> tuple[LineInput, ...]:
    """One synthetic line whose total (tax included) is ``amount`` when ``tax_rate`` is 0."""
    line = LineInput("Synthetic service", "each", Decimal("1"), amount, tax_rate, item_code="SYN-1")
    return (line,)


@dataclass(frozen=True)
class Merchant:
    id: UUID
    receiving_account_id: UUID
    locator: str
    account_number: str


@dataclass(frozen=True)
class Order:
    host_ref_id: str
    intent_id: UUID
    amount_vnd: int
    payment_reference: str
    merchant: Merchant


class _Delivered:
    """First consumer of the fan-out: keeps every event the outbox delivered."""

    def __init__(self) -> None:
        self.events: list[OutboxEventView] = []

    async def publish(self, event: OutboxEventView) -> None:
        self.events.append(event)


class AcceptanceWorld:
    def __init__(
        self,
        engine: AsyncEngine,
        prefix: str,
        root: Path,
        *,
        provider: EInvoiceProvider | None = None,
        clock: Clock | None = None,
        credentials: CredentialResolver | None = None,
    ) -> None:
        """The fake provider on a ``FakeClock`` unless a provider (and its clock and
        credentials) is given, as the sandbox tests do with the real adapter."""
        self.engine = engine
        self.metadata = sa.MetaData()
        self.pm = define_payment_tables(self.metadata, f"{prefix}pm_")
        self.inv = define_invoice_tables(self.metadata, f"{prefix}inv_")
        if provider is None:
            self.clock: Any = FakeClock(datetime.now(UTC))
            self.provider: Any = FakeEInvoiceProvider(self.clock)
            year = today_vn(self.clock.now()).year % 100
            self.provider.set_account_facts(series=(f"C{year:02d}TSE",))
        else:
            self.clock, self.provider = clock or SystemClock(), provider
        self.metrics = RecordingMetrics()
        self.credentials = credentials or StaticCredentials()
        self.store = FileArtifactStore(root)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.invoice = self.build_invoice()
        self.delivered = _Delivered()
        self.publisher = FanOutPublisher(
            [self.delivered, PaymentSettledConsumer(self.invoice.record_settled_payment)]
        )
        self._secret = secrets.token_hex(32)
        self.payment = build_payment_module(
            PaymentModuleConfig(),
            SqlAlchemyUnitOfWorkFactory(self.sessions, self.pm),
            {"sepay": SePayProvider()},
            EnvSecretResolver(environ={"ACCEPTANCE_WEBHOOK_SECRET": self._secret}),
            SystemClock(),
            outbox_publisher=self.publisher,
            template_checklists={"sepay": SePayTemplateChecklist()},
        )
        self.app = create_app(self.payment)
        self._profile: Any = None
        self._profile_active = False

    def build_invoice(self) -> InvoiceModule:
        """An invoice module over the shared database, provider and clock (one per worker)."""
        return build_invoice_module(
            SqlAlchemyInvoiceUnitOfWorkFactory(self.sessions, self.inv),
            self.clock,
            self.metrics,
            provider=self.provider,
            credentials=self.credentials,
            artifact_store=self.store,
        )

    # -- onboarding ------------------------------------------------------------------------

    async def merchant(self, branch: str, account_number: str, *, issuer: bool = True) -> Merchant:
        """A payment merchant with an active SePay connection and, unless ``issuer`` is false,
        an active test issuer account on the invoice side."""
        payment, test = self.payment, PaymentEnvironment.TEST
        merchant = await payment.register_merchant.execute(TENANT, branch, ADMIN)
        account = await payment.register_receiving_account.execute(
            TENANT, merchant.id, test, "VCB", account_number, "SYNTHETIC SELLER", ADMIN
        )
        connection = await payment.register_connection.execute(
            TENANT, merchant.id, "sepay", test, SECRET_REF, ADMIN
        )
        await payment.bind_connection_account.execute(TENANT, connection.id, account.id, ADMIN)
        if self._profile is None:
            draft = await payment.create_reference_profile.execute(
                1, {"bill": "BILL"}, 6, "23456789", ADMIN
            )
            self._profile = draft.profile
        checklist = SePayTemplateChecklist().checklist(connection, self._profile)
        done = {item.key: True for item in checklist.items}
        await payment.record_connection_readiness.execute(
            TENANT, connection.id, 1, test, done, "synthetic-acceptance-evidence", ADMIN
        )
        if not self._profile_active:
            # Activating the profile leaves the first connection ready; a connection made
            # ready under the active profile is active already.
            await payment.activate_reference_profile.execute(1, ADMIN)
            self._profile_active = True
            await payment.set_connection_status.execute(
                TENANT, connection.id, ConnectionStatus.ACTIVE, ADMIN, "acceptance"
            )
        if issuer:
            await self._seed_issuer_account(merchant.id)
        return Merchant(merchant.id, account.id, connection.locator, account_number)

    async def _seed_issuer_account(self, merchant_id: UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                self.inv.issuer_accounts.insert().values(
                    id=uuid4(),
                    tenant_id=TENANT,
                    merchant_id=merchant_id,
                    environment="test",
                    provider="sepay_einvoice",
                    provider_account_id="synthetic-account",
                    template_code="1",
                    invoice_series_template="C{YY}TSE",
                    payment_method="TM/CK",
                    credential_ref="env:SYNTHETIC_CREDENTIAL",
                    buyer_fallback_name="Synthetic fallback buyer",
                    buyer_fallback_address="",
                    issue_date_cross_day_action="issue",
                    status="active",
                )
            )

    # -- orders and payments ---------------------------------------------------------------

    async def order(
        self,
        merchant: Merchant,
        *,
        lines: tuple[LineInput, ...] | None = None,
        amount: int | None = None,
        draft: bool = True,
        draft_lines: tuple[LineInput, ...] | None = None,
        mode: DeliveryMode = DeliveryMode.AUTO,
        host_ref_id: str | None = None,
    ) -> Order:
        """An intent for ``lines`` (or ``amount``); the draft, unless ``draft`` is false, has
        ``draft_lines`` when given (an amount mismatch) and is submitted on its own."""
        lines = lines or lines_for(amount or 100)
        amount_vnd = compute_total(lines)
        host_ref_id = host_ref_id or f"order-{uuid4()}"
        created = await self.payment.create_intent.execute(
            CreateIntentCommand(
                TENANT,
                amount_vnd=amount_vnd,
                host_ref_type=ORDER,
                host_ref_id=host_ref_id,
                idempotency_key=str(uuid4()),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                prefix_name="bill",
                merchant_id=merchant.id,
                receiving_account_id=merchant.receiving_account_id,
            )
        )
        if draft:
            submitted = draft_lines or lines
            await self.invoice.submit_draft.execute(
                SubmitDraftCommand(
                    TENANT,
                    merchant.id,
                    ORDER,
                    host_ref_id,
                    created.intent.id,
                    submitted,
                    compute_total(submitted),
                    delivery_mode=mode,
                )
            )
        intent = created.intent
        return Order(host_ref_id, intent.id, amount_vnd, intent.payment_reference, merchant)

    async def pay(self, order: Order) -> str:
        """POST the signed transfer for ``order`` and settle it; returns the bank reference."""
        code = order.payment_reference
        raw, headers, bank_reference = signed_delivery(
            self._secret, order.merchant.account_number, order.amount_vnd, code, code
        )
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://acceptance.test") as c:
            response = await c.post(
                f"/webhooks/sepay/{order.merchant.locator}", content=raw, headers=headers
            )
        assert response.status_code == 200, response.text
        await self.payment.process_inbox.run_batch()
        return bank_reference

    async def dispatch(self, limit: int = 50) -> int:
        """Deliver pending ``PaymentSettled`` events; returns how many were published."""
        assert self.payment.dispatch_outbox is not None
        results = await self.payment.dispatch_outbox.run_batch(limit)
        return len(results)

    async def drive(self, rounds: int = 8, step: timedelta = timedelta(seconds=61)) -> None:
        """The invoice worker's issue, poll and artifact steps, moving the clock past leases
        and poll delays after each round."""
        module = self.invoice
        assert module.issue_queued and module.poll_pending and module.fetch_artifacts
        for _ in range(rounds):
            await module.issue_queued.run_batch()
            await module.poll_pending.run_batch()
            await module.fetch_artifacts.run_batch()
            self.clock.advance(step)

    # -- reading ---------------------------------------------------------------------------

    async def rows(self, query: Any) -> list[Any]:
        async with self.engine.connect() as conn:
            return list((await conn.execute(query)).all())

    async def scalar(self, query: Any) -> Any:
        async with self.engine.connect() as conn:
            return await conn.scalar(query)

    async def settlement_of(self, order: Order) -> UUID:
        settlements = self.pm.settlements
        settlement_id = await self.scalar(
            sa.select(settlements.c.id).where(settlements.c.intent_id == order.intent_id)
        )
        assert settlement_id is not None, f"no settlement for {order.host_ref_id}"
        return settlement_id

    async def original(self, order: Order) -> Any:
        """The original document of ``order``'s settlement."""
        documents = self.inv.documents
        settlement_id = await self.settlement_of(order)
        [row] = await self.rows(
            sa.select(documents).where(
                documents.c.settlement_id == settlement_id, documents.c.kind == "original"
            )
        )
        return row

    async def reasons(self, document_id: UUID) -> list[tuple[Any, dict[str, Any]]]:
        """``(case, reason item)`` for every reason recorded on the document's cases."""
        cases = self.inv.review_cases
        rows = await self.rows(sa.select(cases).where(cases.c.document_id == document_id))
        return [(case, item) for case in rows for item in case.details["reasons"]]

    async def open_case_id(self, document_id: UUID) -> UUID:
        open_ids = {case.id for case, _ in await self.reasons(document_id) if case.status == "open"}
        assert len(open_ids) == 1, open_ids
        return open_ids.pop()

    async def until(self, predicate: Any, timeout_s: float = 30.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not await predicate():
                await asyncio.sleep(0.02)


@pytest.fixture
async def world(engine: AsyncEngine, tmp_path: Path) -> Any:
    acceptance = AcceptanceWorld(engine, unique_prefix(), tmp_path / "artifacts")
    async with engine.begin() as conn:
        await conn.run_sync(acceptance.metadata.create_all)
    try:
        yield acceptance
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(acceptance.metadata.drop_all)
