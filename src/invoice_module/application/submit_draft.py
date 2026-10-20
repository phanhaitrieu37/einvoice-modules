import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from invoice_module.application.attach_draft import AttachOutcome, attach_draft_to_documents
from invoice_module.domain.document import Buyer, validate_buyer
from invoice_module.domain.enums import DeliveryMode, DraftStatus
from invoice_module.domain.errors import (
    DraftAlreadyConsumed,
    DraftBoundToOtherIntent,
    DraftNotOpen,
    InvalidLine,
)
from invoice_module.domain.money import LineInput, check_declared_total
from invoice_module.ports.clock import Clock
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.unit_of_work import InvoiceUnitOfWork, InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitDraftCommand:
    tenant_id: str
    merchant_id: UUID
    host_ref_type: str
    host_ref_id: str
    intent_id: UUID | None
    lines: tuple[LineInput | Mapping[str, Any], ...]
    declared_total_vnd: int
    buyer: Buyer | None = None
    notes: str | None = None
    delivery_mode: DeliveryMode = field(kw_only=True)

    def __post_init__(self) -> None:
        # The host must choose the mode; an unknown value fails here, before any database work.
        object.__setattr__(self, "delivery_mode", DeliveryMode(self.delivery_mode))


@dataclass(frozen=True)
class SubmitDraftResult:
    draft_id: UUID
    revision: int
    computed_total_vnd: int
    attached: tuple[AttachOutcome, ...]


def build_line_inputs(lines: Sequence[LineInput | Mapping[str, Any]]) -> tuple[LineInput, ...]:
    """Build lines at the ingestion boundary and preserve the source position in errors."""
    built: list[LineInput] = []
    for index, value in enumerate(lines):
        if isinstance(value, LineInput):
            built.append(value)
            continue
        try:
            built.append(LineInput(**value))
        except InvalidLine as exc:
            raise InvalidLine(index, exc.field) from exc
    return tuple(built)


@dataclass(frozen=True)
class _Prepared:
    buyer: Buyer | None
    line_values: list[dict[str, Any]]
    computed_total: int


class SubmitDraft:
    def __init__(self, uow_factory: InvoiceUnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(
        self, cmd: SubmitDraftCommand, uow: InvoiceUnitOfWork | None = None
    ) -> SubmitDraftResult:
        prepared = self._validate(cmd)
        async with uow or self._uow_factory() as active:
            result = await self._store(active, cmd, prepared)
            await active.commit()
            return result

    async def apply(
        self,
        active: InvoiceUnitOfWork,
        cmd: SubmitDraftCommand,
        *,
        actor: str = "system:attach_draft",
        note: str | None = None,
    ) -> SubmitDraftResult:
        """Validate and store the draft inside an active unit of work, without committing.

        ``actor`` and ``note`` are recorded on the review reasons the attach clears.
        """
        return await self._store(active, cmd, self._validate(cmd), actor=actor, note=note)

    @staticmethod
    def _validate(cmd: SubmitDraftCommand) -> "_Prepared":
        if isinstance(cmd.declared_total_vnd, bool) or not isinstance(cmd.declared_total_vnd, int):
            raise TypeError("declared_total_vnd must be an integer VND amount")
        if cmd.declared_total_vnd <= 0:
            raise ValueError("declared_total_vnd must be positive")
        buyer = validate_buyer(cmd.buyer) if cmd.buyer is not None else None
        lines = build_line_inputs(cmd.lines)
        computed_total = check_declared_total(cmd.declared_total_vnd, lines)
        line_values = [
            {
                "name": line.name,
                "unit": line.unit,
                "quantity": str(line.quantity),
                "unit_price_vnd": line.unit_price_vnd,
                "tax_rate": line.tax_rate,
                "discount_vnd": line.discount_vnd,
                "tax_amount_vnd": line.tax_amount_vnd,
                "item_code": line.item_code,
            }
            for line in lines
        ]
        return _Prepared(buyer, line_values, computed_total)

    async def _store(
        self,
        active: InvoiceUnitOfWork,
        cmd: SubmitDraftCommand,
        prepared: "_Prepared",
        *,
        actor: str = "system:attach_draft",
        note: str | None = None,
    ) -> SubmitDraftResult:
        buyer, line_values, computed_total = (
            prepared.buyer,
            prepared.line_values,
            prepared.computed_total,
        )
        await active.lock_host_ref(cmd.tenant_id, cmd.host_ref_type, cmd.host_ref_id)
        existing = await active.drafts.get_by_host_ref(
            cmd.tenant_id, cmd.host_ref_type, cmd.host_ref_id
        )
        revision = 1
        status = DraftStatus.OPEN.value
        draft_id = uuid4()
        if existing is not None:
            draft_id = existing.id
            revision = existing.revision + 1
            if existing.intent_id is not None and cmd.intent_id is None:
                raise DraftBoundToOtherIntent("draft cannot be unbound from its intent")
            if existing.status == DraftStatus.OPEN:
                if existing.intent_id not in (
                    None,
                    cmd.intent_id,
                ) and await active.documents.exists_for_intent(cmd.tenant_id, existing.intent_id):
                    raise DraftBoundToOtherIntent("draft is bound to a settled intent")
            elif existing.status == DraftStatus.CONSUMED and (
                cmd.intent_id is None or cmd.intent_id == existing.intent_id
            ):
                raise DraftAlreadyConsumed("draft is already consumed by this intent")
            if (
                existing.status == DraftStatus.CONSUMED and cmd.intent_id is not None
            ) or existing.status == DraftStatus.DISCARDED:
                await active.drafts.reopen(
                    existing.id, cmd.intent_id, revision, self._clock.now(), cmd.delivery_mode
                )
        values = {
            "id": draft_id,
            "tenant_id": cmd.tenant_id,
            "merchant_id": cmd.merchant_id,
            "host_ref_type": cmd.host_ref_type,
            "host_ref_id": cmd.host_ref_id,
            "intent_id": cmd.intent_id,
            "buyer": buyer.__dict__ if buyer else None,
            "items": line_values,
            "notes": cmd.notes,
            "delivery_mode": cmd.delivery_mode.value,
            "declared_total_vnd": cmd.declared_total_vnd,
            "computed_total_vnd": computed_total,
            "revision": revision,
            "status": status,
            "consumed_by_document_id": None,
        }
        draft = await active.drafts.upsert_open(values, self._clock.now())
        attached = await attach_draft_to_documents(
            active,
            draft,
            active.issuer_accounts.get_active,
            self._clock.now(),
            actor=actor,
            note=note,
        )
        return SubmitDraftResult(draft.id, draft.revision, computed_total, tuple(attached))


class DiscardDraft:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        clock: Clock,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._metrics = metrics or NoOpMetricsSink()

    async def execute(
        self,
        tenant_id: str,
        host_ref_type: str,
        host_ref_id: str,
        actor: str,
        uow: InvoiceUnitOfWork | None = None,
    ) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        async with uow or self._uow_factory() as active:
            await active.lock_host_ref(tenant_id, host_ref_type, host_ref_id)
            draft = await active.drafts.get_by_host_ref(tenant_id, host_ref_type, host_ref_id)
            if draft is None:
                raise DraftNotOpen("draft does not exist")
            if draft.status != DraftStatus.OPEN:
                raise DraftNotOpen("draft is not open")
            await active.drafts.discard(tenant_id, host_ref_type, host_ref_id, self._clock.now())
            await active.commit()
        logger.info(
            "invoice_draft_discarded",
            extra={"draft_id": str(draft.id), "tenant_id": tenant_id, "actor": actor},
        )
        increment_safely(self._metrics, "invoice_drafts_discarded_total", {})
