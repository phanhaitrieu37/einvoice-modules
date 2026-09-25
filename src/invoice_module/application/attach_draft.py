from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow
from invoice_module.adapters.sqlalchemy.repositories.drafts import DraftRow
from invoice_module.domain.document import DraftData, InvoiceDocument
from invoice_module.domain.enums import DocumentStatus, DraftStatus, ReviewReason
from invoice_module.domain.money import LineInput, check_declared_total
from invoice_module.ports.unit_of_work import InvoiceUnitOfWork


@dataclass(frozen=True)
class AttachOutcome:
    document_id: UUID
    status: str


def draft_eligible_for(draft: DraftRow, document: DocumentRow) -> bool:
    return draft.intent_id is None or draft.intent_id == document.intent_id


def _draft_data(draft: DraftRow) -> DraftData:
    lines = tuple(
        LineInput(
            name=str(row["name"]),
            unit=str(row["unit"]),
            quantity=Decimal(str(row["quantity"])),
            unit_price_vnd=int(row["unit_price_vnd"]),
            tax_rate=int(row["tax_rate"]),
            discount_vnd=int(row.get("discount_vnd", 0)),
            tax_amount_vnd=row.get("tax_amount_vnd"),
            item_code=row.get("item_code"),
        )
        for row in draft.items
    )
    from invoice_module.domain.document import Buyer

    buyer = Buyer(**draft.buyer) if draft.buyer else None
    return DraftData(buyer, lines, draft.declared_total_vnd, draft.notes)


async def find_draft_for_document(uow: InvoiceUnitOfWork, document: DocumentRow) -> DraftRow | None:
    if (
        (draft := await uow.drafts.get_by_intent(document.tenant_id, document.intent_id))
        and draft.status == DraftStatus.OPEN
        and draft_eligible_for(draft, document)
    ):
        return draft
    draft = await uow.drafts.get_by_host_ref(
        document.tenant_id, document.host_ref_type, document.host_ref_id
    )
    if draft and draft.status == DraftStatus.OPEN and draft_eligible_for(draft, document):
        return draft
    return None


async def attach_draft_to_documents(
    uow: InvoiceUnitOfWork,
    draft: DraftRow,
    account_lookup: Any,
    now: datetime,
    documents: list[DocumentRow] | tuple[DocumentRow, ...] | None = None,
) -> list[AttachOutcome]:
    candidates = (
        documents
        if documents is not None
        else await uow.documents.find_awaiting_draft(
            draft.tenant_id, draft.host_ref_type, draft.host_ref_id
        )
    )
    outcomes: list[AttachOutcome] = []
    data = _draft_data(draft)
    for document in candidates:
        if not draft_eligible_for(draft, document):
            continue
        account = await account_lookup(
            document.tenant_id, document.merchant_id, document.environment
        )
        if account is None:
            await uow.review_cases.open_once(
                document.tenant_id,
                document.id,
                ReviewReason.ISSUER_ACCOUNT_UNAVAILABLE.value,
                {"draft_found": True},
            )
            continue
        computed = check_declared_total(data.declared_total_vnd, data.lines)
        if computed != document.settled_amount_vnd:
            await uow.documents.update_frozen(
                document.id,
                snapshot=None,
                total_vnd=None,
                buyer_source=None,
                status=DocumentStatus.BLOCKED.value,
                queued_at=None,
                next_attempt_at=None,
                draft_id=draft.id,
            )
            await uow.review_cases.open_once(
                document.tenant_id,
                document.id,
                ReviewReason.AMOUNT_MISMATCH.value,
                {
                    "declared_total_vnd": data.declared_total_vnd,
                    "settled_amount_vnd": document.settled_amount_vnd,
                },
            )
            case = await uow.review_cases.open_reasons(document.id)
            if ReviewReason.DRAFT_MISSING.value in case:
                case_id = await uow.review_cases.get_open_id(document.id)
                assert case_id is not None
                await uow.review_cases.clear_reason(
                    case_id,
                    ReviewReason.DRAFT_MISSING.value,
                    "attach_draft",
                    "system:attach_draft",
                    None,
                    now,
                )
            outcomes.append(AttachOutcome(document.id, DocumentStatus.BLOCKED.value))
            continue
        frozen = InvoiceDocument.from_json(data_to_frozen_json(data, account))
        await uow.documents.update_frozen(
            document.id,
            frozen.to_json(),
            document.settled_amount_vnd,
            frozen.buyer_source.value,
            DocumentStatus.QUEUED.value,
            now,
            now,
            draft.id,
        )
        await uow.drafts.mark_consumed(draft.id, document.id)
        case_id = await uow.review_cases.get_open_id(document.id)
        if case_id is not None:
            for reason in (
                ReviewReason.DRAFT_MISSING.value,
                ReviewReason.AMOUNT_MISMATCH.value,
                ReviewReason.ISSUER_ACCOUNT_UNAVAILABLE.value,
            ):
                if reason in await uow.review_cases.open_reasons(document.id):
                    await uow.review_cases.clear_reason(
                        case_id, reason, "attach_draft", "system:attach_draft", None, now
                    )
        outcomes.append(AttachOutcome(document.id, DocumentStatus.QUEUED.value))
    return outcomes


def data_to_frozen_json(data: DraftData, account: Any) -> dict[str, object]:
    from invoice_module.domain.document import freeze_document

    return freeze_document(data, account).to_json()
