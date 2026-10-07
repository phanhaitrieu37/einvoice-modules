from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow
from invoice_module.application.attach_draft import (
    _draft_data,
    find_draft_for_document,
)
from invoice_module.domain.document import SettledPayment, freeze_document
from invoice_module.domain.enums import DeliveryMode, DocumentStatus, DraftStatus, ReviewReason
from invoice_module.domain.money import check_declared_total
from invoice_module.ports.clock import Clock
from invoice_module.ports.metrics import MetricsSink, NoOpMetricsSink, increment_safely
from invoice_module.ports.unit_of_work import InvoiceUnitOfWork, InvoiceUnitOfWorkFactory


@dataclass(frozen=True)
class RecordResult:
    document_id: UUID
    created: bool


class RecordSettledPayment:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        clock: Clock,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._metrics = metrics or NoOpMetricsSink()

    async def execute(self, settled: SettledPayment) -> RecordResult:
        result: RecordResult
        async with self._uow_factory() as uow:
            await uow.lock_host_ref(settled.tenant_id, settled.host_ref_type, settled.host_ref_id)
            receipt = await uow.receipts.get(settled.event_id)
            if receipt is not None:
                result = RecordResult(receipt["document_id"], False)
                await uow.commit()
                self._record_metric("duplicate")
                return result

            existing = await uow.documents.get_original(settled.tenant_id, settled.settlement_id)
            if existing is not None:
                await uow.receipts.insert_once(self._receipt(settled, existing.id))
                await uow.commit()
                self._record_metric("duplicate")
                return RecordResult(existing.id, False)

            account = await uow.issuer_accounts.get_active(
                settled.tenant_id, settled.merchant_id, settled.environment
            )
            now = self._clock.now()
            document_id = uuid4()
            prior = await uow.documents.has_other_original_for_host_ref(
                settled.tenant_id,
                settled.host_ref_type,
                settled.host_ref_id,
                settled.settlement_id,
            )
            draft = None
            if account is None:
                draft_found = await self._draft_exists(uow, settled)
                try:
                    await uow.documents.insert(
                        self._document_row(
                            settled,
                            document_id,
                            now,
                            None,
                            None,
                            DocumentStatus.BLOCKED.value,
                            None,
                        )
                    )
                    await uow.review_cases.open_once(
                        settled.tenant_id,
                        document_id,
                        ReviewReason.ISSUER_ACCOUNT_UNAVAILABLE.value,
                        {"draft_found": draft_found},
                    )
                    if prior:
                        await uow.review_cases.open_once(
                            settled.tenant_id,
                            document_id,
                            ReviewReason.DUPLICATE_SETTLEMENT.value,
                            {"other_settlement_ids": [str(value) for value in prior]},
                        )
                    await uow.receipts.insert_once(self._receipt(settled, document_id))
                    await uow.commit()
                except IntegrityError:
                    await uow.rollback()
                    await uow.lock_host_ref(
                        settled.tenant_id, settled.host_ref_type, settled.host_ref_id
                    )
                    winner = await uow.documents.get_original(
                        settled.tenant_id, settled.settlement_id
                    )
                    if winner is None:
                        raise
                    await uow.receipts.insert_once(self._receipt(settled, winner.id))
                    await uow.commit()
                    self._record_metric("duplicate")
                    return RecordResult(winner.id, False)
                self._record_metric("blocked")
                return RecordResult(document_id, True)

            # Construct a row to apply the common intent-first draft lookup rule.
            row = DocumentRow(
                id=document_id,
                tenant_id=settled.tenant_id,
                merchant_id=settled.merchant_id,
                environment=settled.environment.value,
                settlement_id=settled.settlement_id,
                intent_id=settled.intent_id,
                host_ref_type=settled.host_ref_type,
                host_ref_id=settled.host_ref_id,
                issuer_account_id=account.id,
                draft_id=None,
                settled_amount_vnd=settled.amount_vnd,
                settled_at=settled.settled_at,
                status=DocumentStatus.NEEDS_DRAFT,
                document=None,
                delivery_mode=None,
                approved_at=None,
                approved_by=None,
                approved_snapshot_sha256=None,
                approved_reference_code=None,
            )
            draft = await find_draft_for_document(uow, row)
            document_snapshot = None
            total_vnd = None
            buyer_source = None
            delivery_mode = None
            draft_id = None
            queued_at = None
            next_attempt_at = None
            status = DocumentStatus.NEEDS_DRAFT.value
            amount_mismatch = False
            if draft is not None:
                data = _draft_data(draft)
                computed = check_declared_total(data.declared_total_vnd, data.lines)
                if computed != settled.amount_vnd:
                    status = DocumentStatus.BLOCKED.value
                    amount_mismatch = True
                    draft_id = draft.id
                else:
                    frozen = freeze_document(data, account)
                    document_snapshot = frozen.to_json()
                    total_vnd = frozen.total_vnd
                    buyer_source = frozen.buyer_source.value
                    delivery_mode = draft.delivery_mode
                    draft_id = draft.id
                    queued_at = now
                    next_attempt_at = now
                    status = DocumentStatus.QUEUED.value

            try:
                await uow.documents.insert(
                    self._document_row(
                        settled,
                        document_id,
                        now,
                        account.id,
                        draft_id,
                        status,
                        document_snapshot,
                        total_vnd=total_vnd,
                        buyer_source=buyer_source,
                        delivery_mode=delivery_mode,
                        queued_at=queued_at,
                        next_attempt_at=next_attempt_at,
                    )
                )
                if draft is not None and document_snapshot is not None:
                    await uow.drafts.mark_consumed(draft.id, document_id)
                if amount_mismatch:
                    await uow.review_cases.open_once(
                        settled.tenant_id,
                        document_id,
                        ReviewReason.AMOUNT_MISMATCH.value,
                        {
                            "declared_total_vnd": draft.declared_total_vnd,
                            "settled_amount_vnd": settled.amount_vnd,
                        },
                    )
                if prior:
                    await uow.review_cases.open_once(
                        settled.tenant_id,
                        document_id,
                        ReviewReason.DUPLICATE_SETTLEMENT.value,
                        {"other_settlement_ids": [str(value) for value in prior]},
                    )
                await uow.receipts.insert_once(self._receipt(settled, document_id))
                await uow.commit()
                self._record_metric(status)
                return RecordResult(document_id, True)
            except IntegrityError:
                await uow.rollback()
                # Rollback clears owned lock state; re-acquire before retrying reads.
                await uow.lock_host_ref(
                    settled.tenant_id, settled.host_ref_type, settled.host_ref_id
                )
                winner = await uow.documents.get_original(settled.tenant_id, settled.settlement_id)
                if winner is None:
                    raise
                await uow.receipts.insert_once(self._receipt(settled, winner.id))
                await uow.commit()
                self._record_metric("duplicate")
                return RecordResult(winner.id, False)

    async def _draft_exists(self, uow: InvoiceUnitOfWork, settled: SettledPayment) -> bool:
        # Read only for review context; no-account never consumes the draft.
        drafts = uow.drafts
        by_intent = await drafts.get_by_intent(settled.tenant_id, settled.intent_id)
        if (
            by_intent is not None
            and getattr(by_intent.status, "value", by_intent.status) == DraftStatus.OPEN.value
            and by_intent.merchant_id == settled.merchant_id
        ):
            return True
        draft = await drafts.get_by_host_ref(
            settled.tenant_id, settled.host_ref_type, settled.host_ref_id
        )
        return bool(
            draft
            and getattr(draft.status, "value", draft.status) == DraftStatus.OPEN.value
            and draft.merchant_id == settled.merchant_id
            and (draft.intent_id is None or draft.intent_id == settled.intent_id)
        )

    @staticmethod
    def _document_row(
        settled: SettledPayment,
        document_id: UUID,
        now: datetime,
        issuer_account_id: UUID | None,
        draft_id: UUID | None,
        status: str,
        snapshot: dict[str, object] | None,
        *,
        total_vnd: int | None = None,
        buyer_source: str | None = None,
        delivery_mode: DeliveryMode | None = None,
        queued_at: datetime | None = None,
        next_attempt_at: datetime | None = None,
    ) -> dict[str, object]:
        return {
            "id": document_id,
            "tenant_id": settled.tenant_id,
            "merchant_id": settled.merchant_id,
            "environment": settled.environment.value,
            "issuer_account_id": issuer_account_id,
            "kind": "original",
            "settlement_id": settled.settlement_id,
            "intent_id": settled.intent_id,
            "host_ref_type": settled.host_ref_type,
            "host_ref_id": settled.host_ref_id,
            "draft_id": draft_id,
            "reference_code": str(settled.settlement_id),
            "document": snapshot,
            "total_vnd": total_vnd,
            "settled_amount_vnd": settled.amount_vnd,
            "settled_at": settled.settled_at,
            "buyer_source": buyer_source,
            "delivery_mode": delivery_mode.value if delivery_mode is not None else None,
            "status": status,
            "queued_at": queued_at,
            "next_attempt_at": next_attempt_at,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _receipt(settled: SettledPayment, document_id: UUID) -> dict[str, object]:
        return {
            "event_id": settled.event_id,
            "tenant_id": settled.tenant_id,
            "merchant_id": settled.merchant_id,
            "environment": settled.environment.value,
            "settlement_id": settled.settlement_id,
            "intent_id": settled.intent_id,
            "transaction_id": settled.transaction_id,
            "host_ref_type": settled.host_ref_type,
            "host_ref_id": settled.host_ref_id,
            "amount_vnd": settled.amount_vnd,
            "origin": settled.origin,
            "settled_at": settled.settled_at,
            "document_id": document_id,
        }

    def _record_metric(self, status: str) -> None:
        increment_safely(self._metrics, "invoice_settlements_recorded_total", {"status": status})
