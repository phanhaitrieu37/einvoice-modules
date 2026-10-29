"""A worker test world on real PostgreSQL: settled documents, a fake provider and a fake clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider
from tests.fakes.recording import (
    CrashableUnitOfWorkFactory,
    FakeClock,
    RecordingMetrics,
    StaticCredentials,
)

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables
from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWorkFactory
from invoice_module.adapters.storage import FileArtifactStore
from invoice_module.application.config import VN_TZ, InvoiceModuleConfig
from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.builder import InvoiceModule, build_invoice_module
from invoice_module.domain import state
from invoice_module.domain.document import Buyer, SettledPayment
from invoice_module.domain.enums import DeliveryMode, Environment
from invoice_module.domain.money import LineInput

# Wednesday 21 October 2026, 10:00 in Asia/Saigon.
START = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
TENANT = "tenant-test"


def vn(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0,
       microsecond: int = 0) -> datetime:  # fmt: skip
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=VN_TZ)


def lines_for(amount: int, name: str = "Synthetic service") -> tuple[LineInput, ...]:
    return (LineInput(name, "each", Decimal("1"), amount, 0, item_code="SYN-ITEM-1"),)


@dataclass(frozen=True)
class Settled:
    document_id: UUID
    settlement_id: UUID
    intent_id: UUID
    host_ref_id: str
    merchant_id: UUID

    @property
    def reference(self) -> str:
        return str(self.settlement_id)


class World:
    def __init__(self, engine: AsyncEngine, tables: InvoiceTables, root: Path) -> None:
        self.engine, self.tables = engine, tables
        self.clock = FakeClock(START)
        self.provider = FakeEInvoiceProvider(self.clock)
        self.metrics = RecordingMetrics()
        self.credentials = StaticCredentials()
        self.store = FileArtifactStore(root)
        self.config = InvoiceModuleConfig()
        self.crash = CrashableUnitOfWorkFactory(
            SqlAlchemyInvoiceUnitOfWorkFactory(
                async_sessionmaker(engine, expire_on_commit=False), tables
            )
        )
        self.merchant_id = uuid4()
        self.account_id = uuid4()
        self.module = self.build()

    def build(self) -> InvoiceModule:
        """A fresh module over the same database, as after a process restart."""
        return build_invoice_module(
            self.crash,
            self.clock,
            self.metrics,
            provider=self.provider,
            credentials=self.credentials,
            artifact_store=self.store,
            config=self.config,
        )

    def restart(self) -> InvoiceModule:
        self.module = self.build()
        return self.module

    # -- seeding ---------------------------------------------------------------------------

    async def seed_account(self, *, cross_day: str = "issue", **over: Any) -> None:
        values = {
            "id": self.account_id,
            "tenant_id": TENANT,
            "merchant_id": self.merchant_id,
            "environment": Environment.TEST.value,
            "provider": "sepay_einvoice",
            "provider_account_id": "synthetic-account",
            "template_code": "1",
            "invoice_series_template": "C{YY}TSE",
            "payment_method": "TM/CK",
            "credential_ref": "env:SYNTHETIC_CREDENTIAL",
            "buyer_fallback_name": "Synthetic fallback buyer",
            "buyer_fallback_address": "",
            "issue_date_cross_day_action": cross_day,
            "status": "active",
        } | over
        async with self.engine.begin() as conn:
            await conn.execute(self.tables.issuer_accounts.insert().values(**values))

    async def set_cross_day(self, action: str) -> None:
        table = self.tables.issuer_accounts
        async with self.engine.begin() as conn:
            await conn.execute(
                table.update()
                .where(table.c.id == self.account_id)
                .values(issue_date_cross_day_action=action)
            )

    def draft_command(
        self,
        host_ref_id: str,
        intent_id: UUID | None,
        *,
        amount: int = 100,
        mode: DeliveryMode = DeliveryMode.AUTO,
        name: str = "Synthetic service",
        buyer: Buyer | None = None,
        merchant_id: UUID | None = None,
    ) -> SubmitDraftCommand:
        return SubmitDraftCommand(
            TENANT,
            merchant_id or self.merchant_id,
            "order",
            host_ref_id,
            intent_id,
            lines_for(amount, name),
            amount,
            buyer=buyer,
            delivery_mode=mode,
        )

    async def settle(
        self,
        *,
        mode: DeliveryMode = DeliveryMode.AUTO,
        amount: int = 100,
        draft: bool = True,
        draft_amount: int | None = None,
        host_ref_id: str | None = None,
        name: str = "Synthetic service",
    ) -> Settled:
        host_ref_id = host_ref_id or f"order-{uuid4()}"
        intent_id, settlement_id = uuid4(), uuid4()
        if draft:
            await self.module.submit_draft.execute(
                self.draft_command(
                    host_ref_id, intent_id, amount=draft_amount or amount, mode=mode, name=name
                )
            )
        recorded = await self.module.record_settled_payment.execute(
            SettledPayment(
                event_id=uuid4(),
                tenant_id=TENANT,
                merchant_id=self.merchant_id,
                environment=Environment.TEST,
                intent_id=intent_id,
                settlement_id=settlement_id,
                transaction_id=uuid4(),
                amount_vnd=amount,
                settled_at=self.clock.now(),
                origin="auto",
                host_ref_type="order",
                host_ref_id=host_ref_id,
            )
        )
        return Settled(
            recorded.document_id, settlement_id, intent_id, host_ref_id, self.merchant_id
        )

    async def settle_again(
        self, first: Settled, *, amount: int = 100, intent_id: UUID | None = None
    ) -> Settled:
        """A second settlement for the same order (a duplicate payment)."""
        intent_id, settlement_id = intent_id or uuid4(), uuid4()
        recorded = await self.module.record_settled_payment.execute(
            SettledPayment(
                event_id=uuid4(),
                tenant_id=TENANT,
                merchant_id=self.merchant_id,
                environment=Environment.TEST,
                intent_id=intent_id,
                settlement_id=settlement_id,
                transaction_id=uuid4(),
                amount_vnd=amount,
                settled_at=self.clock.now(),
                origin="auto",
                host_ref_type="order",
                host_ref_id=first.host_ref_id,
            )
        )
        return Settled(
            recorded.document_id, settlement_id, intent_id, first.host_ref_id, self.merchant_id
        )

    async def approve(self, settled: Settled, actor: str = "accountant-a") -> Any:
        return await self.module.approve_draft_invoice.execute(settled.document_id, actor)

    # -- running ---------------------------------------------------------------------------

    async def issue(self, times: int = 1) -> list[Any]:
        results: list[Any] = []
        for _ in range(times):
            assert self.module.issue_queued is not None
            results += await self.module.issue_queued.run_batch()
        return results

    async def poll(self, times: int = 1) -> list[Any]:
        results: list[Any] = []
        for _ in range(times):
            assert self.module.poll_pending is not None
            results += await self.module.poll_pending.run_batch()
        return results

    async def artifacts(self) -> list[Any]:
        assert self.module.fetch_artifacts is not None
        return await self.module.fetch_artifacts.run_batch()

    async def drive(self, rounds: int = 8, step: timedelta = timedelta(seconds=61)) -> None:
        """Run issue and poll loops, moving the clock past leases and poll delays each round."""
        for _ in range(rounds):
            await self.issue()
            await self.poll()
            self.clock.advance(step)

    def advance(self, delta: timedelta) -> datetime:
        return self.clock.advance(delta)

    def past_lease(self) -> datetime:
        return self.clock.advance(timedelta(seconds=self.config.lease_seconds + 1))

    # -- reading ---------------------------------------------------------------------------

    async def doc(self, document_id: UUID) -> Any:
        table = self.tables.documents
        async with self.engine.connect() as conn:
            return (await conn.execute(sa.select(table).where(table.c.id == document_id))).one()

    async def cases(self, document_id: UUID) -> list[Any]:
        table = self.tables.review_cases
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(table)
                    .where(table.c.document_id == document_id)
                    .order_by(table.c.opened_at, table.c.id)
                )
            ).all()

    async def open_case(self, document_id: UUID) -> Any:
        open_cases = [case for case in await self.cases(document_id) if case.status == "open"]
        assert len(open_cases) == 1, open_cases
        return open_cases[0]

    async def reasons(self, document_id: UUID) -> list[tuple[str, str, str | None]]:
        return [
            (item["reason"], item["status"], item.get("resolution"))
            for case in await self.cases(document_id)
            for item in case.details["reasons"]
        ]

    async def open_reasons(self, document_id: UUID) -> list[str]:
        return [reason for reason, status, _ in await self.reasons(document_id) if status == "open"]

    async def reason_details(self, document_id: UUID, reason: str) -> dict[str, Any]:
        matches = [
            item
            for case in await self.cases(document_id)
            for item in case.details["reasons"]
            if item["reason"] == reason
        ]
        assert matches, f"no {reason} recorded"
        return matches[-1]["details"]

    async def set_columns(self, document_id: UUID, **values: Any) -> None:
        table = self.tables.documents
        async with self.engine.begin() as conn:
            await conn.execute(table.update().where(table.c.id == document_id).values(**values))

    async def resolve(self, document_id: UUID, resolution: str, actor: str = "operator-a",
                      note: str | None = None, **kwargs: Any) -> Any:  # fmt: skip
        case = await self.open_case(document_id)
        return await self.module.resolve_review.execute(case.id, resolution, actor, note, **kwargs)


class TransitionRecorder:
    """Wraps ``ensure_transition`` and records every edge the code asks for."""

    def __init__(self, monkeypatch: Any) -> None:
        self.edges: list[tuple[str, str]] = []
        original = state.ensure_transition

        def recording(current: Any, target: Any) -> None:
            self.edges.append((str(current), str(target)))
            original(current, target)

        monkeypatch.setattr(state, "ensure_transition", recording)


def vn_date(moment: datetime) -> date:
    return moment.astimezone(VN_TZ).date()
