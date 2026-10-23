"""Programmable in-memory SePay eInvoice provider for worker tests.

Every create issues in one step (a provider draft is never requested). The provider records the
issue date sent in the request and refuses a date earlier than its own Asia/Saigon date at the
moment it receives the request, as the sandbox did with ``400 ISSUED_DATE_INVALID``. Create
requests are built by the SePay adapter's own payload builder, so a frozen request has the shape
the provider receives.

Counting: ``calls`` holds one entry per transport request (a POST that reached the provider,
whatever its answer) and per read; ``effective_creates`` counts invoices that really exist at the
provider for a reference (a job that ends ``Failed`` is not an invoice).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from tests.fakes.recording import Crash

from invoice_module.adapters.sepay.payload import build_create_payload
from invoice_module.application.config import today_vn
from invoice_module.domain.document import InvoiceDocument
from invoice_module.ports.provider import (
    Accepted,
    AccountFacts,
    Artifacts,
    ClientCredentials,
    DocumentExists,
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderInvoice,
    ProviderProgress,
    ProviderRejected,
    ProviderTransient,
    QuotaExhausted,
    SendAborted,
)

# Outcomes of one create, in the order a test scripts them.
ACCEPT = "accept"
TRANSIENT = "transient"  # the provider answers 5xx; nothing is created
LOST_RESPONSE = "lost_response"  # created at the provider; the client times out
CRASH_AFTER_RECEIVE = "crash_after_receive"  # created; the worker process dies
QUOTA = "quota"  # 500 QUOTA_HAS_BEEN_USERD_UP: the POST counts, nothing is created
AUTH_FAILED = "auth_failed"  # 403 on the POST
AUTH_FAILED_BEFORE_SEND = "auth_failed_before_send"  # token request fails; no POST
CRASH_BEFORE_SEND = "crash_before_send"  # process dies before the send-boundary hook
CRASH_AFTER_MARK = "crash_after_mark"  # hook granted the POST; process dies before transport
CONFLICT = "conflict"  # forced 409
BLOCK = "block"  # the POST waits until the test releases it

# The provider's quota code, spelled exactly as the API returns it.
QUOTA_CODE = "QUOTA_HAS_BEEN_USERD_UP"


def rejected(code: str) -> tuple[str, str]:
    """The provider refuses the create with ``code``; nothing is created."""
    return ("rejected", code)


def account_status_rejected(code: str) -> tuple[str, str]:
    """A refusal about the account or seller store (for example ``BILLING_IS_UNPAID``), passed
    through by the adapter as the provider's code."""
    return rejected(code)


def raises(error: Exception) -> tuple[str, Exception]:
    """The create raises exactly ``error`` once the POST was sent; nothing is created."""
    return ("raise", error)


@dataclass(frozen=True)
class Call:
    op: str
    reference_code: str
    at: datetime
    payload: Mapping[str, Any] | None = None


@dataclass
class FakeInvoice:
    reference_code: str
    issued_date: date | None
    total_amount_vnd: int | None
    state: str = "success"
    pending_polls: int = 0
    provider_message: str | None = None
    invoice_number: str | None = None
    provider_invoice_id: str | None = None
    # A failed record that does not consume the reference lets a new create succeed.
    consumes_reference: bool = True


@dataclass
class _Gate:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    deliver: bool = True
    respond: str = "timeout"


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class FakeEInvoiceProvider:
    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.calls: list[Call] = []
        self.requests_received: dict[str, Mapping[str, Any]] = {}
        self.effective_creates: Counter[str] = Counter()
        self.invoices: dict[str, FakeInvoice] = {}
        self.reconciled: set[str] = set()
        self._create_scripts: dict[str, deque[Any]] = {}
        self._any_create: deque[Any] = deque()
        self._lookup_scripts: dict[str, deque[Any]] = {}
        self._check_scripts: dict[str, deque[Any]] = {}
        self._new_invoice: dict[str, dict[str, Any]] = {}
        self._receive_delay: dict[str, deque[timedelta]] = {}
        self._omit_issued_date: dict[str, set[str]] = {}
        self._artifact_failures: Counter[str] = Counter()
        self._gates: dict[str, _Gate] = {}
        self._built: dict[str, tuple[int, date]] = {}
        self._next_hooks: dict[str, deque[Callable[[], Awaitable[None] | None]]] = {}
        self._prepare_delay: dict[str, deque[timedelta]] = {}
        self._prepare_hooks: deque[Callable[[], Awaitable[None]]] = deque()
        self._number = 0

    # -- scripting -----------------------------------------------------------------------

    def script_create(self, reference_code: str, *outcomes: Any) -> None:
        self._create_scripts.setdefault(reference_code, deque()).extend(outcomes)

    def script_any_create(self, *outcomes: Any) -> None:
        self._any_create.extend(outcomes)

    def script_lookup(self, reference_code: str, *results: Any) -> None:
        """Each item is ``None``, ``TRANSIENT``, ``AUTH_FAILED`` or a ``ProviderInvoice``
        returned once."""
        self._lookup_scripts.setdefault(reference_code, deque()).extend(results)

    def script_check(self, reference_code: str, *results: Any) -> None:
        """Each item is ``TRANSIENT`` or a ``ProviderProgress`` returned once."""
        self._check_scripts.setdefault(reference_code, deque()).extend(results)

    def new_invoice(
        self,
        reference_code: str,
        *,
        pending_polls: int = 0,
        final_state: str = "success",
        message: str | None = None,
        total_amount_vnd: int | None = None,
    ) -> None:
        """How the next invoice created for ``reference_code`` behaves."""
        self._new_invoice[reference_code] = {
            "pending_polls": pending_polls,
            "state": final_state,
            "provider_message": message,
            "total_amount_vnd": total_amount_vnd,
        }

    def receive_delay(self, reference_code: str, *delays: timedelta) -> None:
        """Clock time that passes between the send boundary and the provider receiving."""
        self._receive_delay.setdefault(reference_code, deque()).extend(delays)

    def omit_issued_date(self, reference_code: str, *ops: str) -> None:
        self._omit_issued_date[reference_code] = set(ops or ("check", "lookup"))

    def fail_artifacts(self, reference_code: str, times: int = 1) -> None:
        self._artifact_failures[reference_code] += times

    def seed_invoice(self, invoice: FakeInvoice) -> None:
        """A record that already exists at the provider (for example an earlier failed job)."""
        if invoice.invoice_number is None and invoice.state == "success":
            invoice.invoice_number = self._next_number()
        invoice.provider_invoice_id = invoice.provider_invoice_id or f"inv-{invoice.reference_code}"
        self.invoices[invoice.reference_code] = invoice

    def reconcile_at_portal(self, reference_code: str) -> None:
        """An operator settled the reference at the portal: it can never create an invoice."""
        self.reconciled.add(reference_code)

    def gate(self, reference_code: str) -> _Gate:
        return self._gates.setdefault(reference_code, _Gate())

    def release(self, reference_code: str, *, deliver: bool = True, respond: str = "timeout"):
        gate = self.gate(reference_code)
        gate.deliver, gate.respond = deliver, respond
        gate.event.set()

    def on_next(self, op: str, hook: Callable[[], Awaitable[None] | None]) -> None:
        """Run ``hook`` once, right after the next ``op`` was answered by the provider."""
        self._next_hooks.setdefault(op, deque()).append(hook)

    def delay_before_send(self, reference_code: str, *delays: timedelta) -> None:
        """Clock time spent on token and rate limiter before the send-boundary hook runs."""
        self._prepare_delay.setdefault(reference_code, deque()).extend(delays)

    def on_next_prepare(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Run ``hook`` once inside the next create, before its send-boundary hook."""
        self._prepare_hooks.append(hook)

    def posts(self, reference_code: str | None = None) -> list[Call]:
        return [
            call
            for call in self.calls
            if call.op == "create"
            and (reference_code is None or call.reference_code == reference_code)
        ]

    def ops(self) -> list[str]:
        return [call.op for call in self.calls]

    # -- port ------------------------------------------------------------------------------

    def build_create_payload(
        self,
        account: IssuerAccountView,
        doc: InvoiceDocument,
        reference_code: str,
        issued_date: date,
    ) -> dict[str, object]:
        payload = build_create_payload(account, doc, reference_code, issued_date)
        self._built[_canonical(payload)] = (doc.total_vnd, issued_date)
        return payload

    async def create(
        self,
        account: IssuerAccountView,
        creds: ClientCredentials,
        request: Mapping[str, object],
        *,
        before_send: Any = None,
    ) -> Accepted:
        reference_code = str(request["reference_code"])
        outcome = self._next_create(reference_code)
        await asyncio.sleep(0)  # token and rate limiter
        for delay in self._pop(self._prepare_delay, reference_code):
            self.clock.advance(delay)
        if self._prepare_hooks:
            await self._prepare_hooks.popleft()()
        if outcome == AUTH_FAILED_BEFORE_SEND:
            raise ProviderAuthFailed("token request refused")
        if outcome == CRASH_BEFORE_SEND:
            raise Crash("crash before the send boundary")
        if before_send is not None and not await before_send():
            raise SendAborted("send boundary refused")
        if outcome == CRASH_AFTER_MARK:
            raise Crash("crash after the POST was granted")
        assert request.get("is_draft", False) is False, "a provider draft was requested"
        sent_at = self.clock.now()
        self.calls.append(Call("create", reference_code, sent_at, dict(request)))
        for delay in self._pop(self._receive_delay, reference_code):
            self.clock.advance(delay)
        if outcome == BLOCK:
            gate = self.gate(reference_code)
            await gate.event.wait()
            if gate.deliver:
                self._receive(reference_code, request)
            await self._fire("create")
            if gate.respond == "accepted" and gate.deliver:
                return Accepted(tracking_code=reference_code)
            raise ProviderTransient()
        if outcome == TRANSIENT:
            await self._fire("create")
            raise ProviderTransient()
        if outcome == QUOTA:
            await self._fire("create")
            raise QuotaExhausted(QUOTA_CODE)
        if outcome == AUTH_FAILED:
            await self._fire("create")
            raise ProviderAuthFailed("forbidden")
        if outcome == CONFLICT:
            await self._fire("create")
            raise DocumentExists("forced conflict")
        if isinstance(outcome, tuple) and outcome[0] == "rejected":
            await self._fire("create")
            raise ProviderRejected(outcome[1])
        if isinstance(outcome, tuple) and outcome[0] == "raise":
            await self._fire("create")
            raise outcome[1]
        try:
            self._receive(reference_code, request)
        except (DocumentExists, ProviderRejected):
            if outcome in (LOST_RESPONSE, CRASH_AFTER_RECEIVE):
                await self._fire("create")
                if outcome == CRASH_AFTER_RECEIVE:
                    raise Crash("crash after the provider answered") from None
                raise ProviderTransient() from None
            await self._fire("create")
            raise
        await self._fire("create")
        if outcome == LOST_RESPONSE:
            raise ProviderTransient()
        if outcome == CRASH_AFTER_RECEIVE:
            raise Crash("crash after the provider received the request")
        return Accepted(tracking_code=reference_code)

    async def check_create(
        self, account: IssuerAccountView, creds: ClientCredentials, tracking_code: str
    ) -> ProviderProgress:
        self.calls.append(Call("check_create", tracking_code, self.clock.now()))
        scripted = self._pop_one(self._check_scripts, tracking_code)
        try:
            if scripted is TRANSIENT:
                raise ProviderTransient()
            if scripted is not None:
                return scripted
            invoice = self.invoices.get(tracking_code)
            if invoice is None:
                raise ProviderTransient()
            if invoice.pending_polls > 0:
                invoice.pending_polls -= 1
                return ProviderProgress(state="pending")
            return ProviderProgress(
                state=invoice.state,  # type: ignore[arg-type]
                invoice_number=invoice.invoice_number,
                provider_invoice_id=invoice.provider_invoice_id,
                issued_date=self._date_for("check", invoice),
                provider_message=invoice.provider_message,
            )
        finally:
            await self._fire("check_create")

    async def find_by_reference(
        self, account: IssuerAccountView, creds: ClientCredentials, reference_code: str
    ) -> ProviderInvoice | None:
        self.calls.append(Call("find_by_reference", reference_code, self.clock.now()))
        try:
            queue = self._lookup_scripts.get(reference_code)
            if queue:
                scripted = queue.popleft()
                if scripted is TRANSIENT:
                    raise ProviderTransient()
                if scripted is AUTH_FAILED:
                    raise ProviderAuthFailed("forbidden")
                return scripted
            invoice = self.invoices.get(reference_code)
            if invoice is None:
                return None
            state = "pending" if invoice.pending_polls > 0 else invoice.state
            return ProviderInvoice(
                reference_code=reference_code,
                state=state,  # type: ignore[arg-type]
                tracking_code=reference_code,
                provider_invoice_id=invoice.provider_invoice_id,
                invoice_number=invoice.invoice_number if state == "success" else None,
                total_amount_vnd=invoice.total_amount_vnd,
                issued_date=self._date_for("lookup", invoice) if state == "success" else None,
                provider_message=invoice.provider_message,
            )
        finally:
            await self._fire("find_by_reference")

    async def fetch_artifacts(
        self,
        account: IssuerAccountView,
        creds: ClientCredentials,
        reference_code: str,
        provider_invoice_id: str,
    ) -> Artifacts:
        self.calls.append(Call("fetch_artifacts", reference_code, self.clock.now()))
        try:
            if self._artifact_failures[reference_code] > 0:
                self._artifact_failures[reference_code] -= 1
                raise ProviderTransient()
            return Artifacts(
                pdf=f"%PDF-1.4 synthetic {reference_code}".encode(),
                xml=f"<invoice reference='{reference_code}'/>".encode(),
            )
        finally:
            await self._fire("fetch_artifacts")

    async def describe_account(
        self, account: IssuerAccountView, creds: ClientCredentials
    ) -> AccountFacts:
        self.calls.append(Call("describe_account", account.provider_account_id, self.clock.now()))
        return AccountFacts(account.provider_account_id, ("1",), ("C26TSE",))

    async def adjust(self, account: Any, creds: Any, request: Mapping[str, object]) -> Accepted:
        self.calls.append(Call("adjust", str(request.get("reference_code")), self.clock.now()))
        raise NotImplementedError

    async def replace(self, account: Any, creds: Any, request: Mapping[str, object]) -> Accepted:
        self.calls.append(Call("replace", str(request.get("reference_code")), self.clock.now()))
        raise NotImplementedError

    def build_adjust_payload(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise NotImplementedError

    def build_replace_payload(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise NotImplementedError

    # -- internals -------------------------------------------------------------------------

    def _next_create(self, reference_code: str) -> Any:
        queue = self._create_scripts.get(reference_code)
        if queue:
            return queue.popleft()
        if self._any_create:
            return self._any_create.popleft()
        return ACCEPT

    def _receive(self, reference_code: str, request: Mapping[str, Any]) -> None:
        """The provider processes a request it received: 409, date refusal or a new invoice."""
        self.requests_received[reference_code] = dict(request)
        existing = self.invoices.get(reference_code)
        if reference_code in self.reconciled or (
            existing is not None and (existing.state != "failed" or existing.consumes_reference)
        ):
            raise DocumentExists("reference already used")
        total, issued_date = self._request_facts(request)
        if issued_date < today_vn(self.clock.now()):
            raise ProviderRejected("issued_date_invalid")
        settings = self._new_invoice.pop(reference_code, {})
        state = settings.get("state", "success")
        invoice = FakeInvoice(
            reference_code=reference_code,
            issued_date=issued_date,
            total_amount_vnd=settings.get("total_amount_vnd") or total,
            state=state,
            pending_polls=settings.get("pending_polls", 0),
            provider_message=settings.get("provider_message"),
            invoice_number=self._next_number() if state == "success" else None,
            provider_invoice_id=f"inv-{reference_code}",
        )
        self.invoices[reference_code] = invoice
        if state == "success":  # a job that ends Failed leaves no invoice behind
            self.effective_creates[reference_code] += 1

    def _request_facts(self, request: Mapping[str, Any]) -> tuple[int | None, date]:
        built = self._built.get(_canonical(request))
        if built is not None:
            return built
        return None, date.fromisoformat(str(request["issued_date"])[:10])

    def _date_for(self, op: str, invoice: FakeInvoice) -> date | None:
        if op in self._omit_issued_date.get(invoice.reference_code, set()):
            return None
        return invoice.issued_date

    def _next_number(self) -> str:
        self._number += 1
        return f"{self._number:08d}"

    async def _fire(self, op: str) -> None:
        hooks = self._next_hooks.get(op)
        if hooks:
            result = hooks.popleft()()
            if result is not None:
                await result

    @staticmethod
    def _pop(store: dict[str, deque[Any]], key: str) -> list[Any]:
        queue = store.get(key)
        return [queue.popleft()] if queue else []

    @staticmethod
    def _pop_one(store: dict[str, deque[Any]], key: str) -> Any:
        queue = store.get(key)
        return queue.popleft() if queue else None


def failed_invoice(reference_code: str, message: str | None, **over: Any) -> ProviderInvoice:
    return replace(
        ProviderInvoice(reference_code=reference_code, state="failed", provider_message=message),
        **over,
    )
