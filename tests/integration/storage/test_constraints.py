from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.support.db import Rows

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
async def rows(engine: AsyncEngine, inv_tables: InvoiceTables) -> AsyncIterator[Rows]:
    async with engine.connect() as conn:
        await conn.begin()
        try:
            yield Rows(conn, inv_tables)
        finally:
            await conn.rollback()


async def account(rows: Rows, **over: object) -> dict[str, object]:
    return await rows.issuer_account(**over)


async def original(rows: Rows, **over: object) -> dict[str, object]:
    if "issuer_account_id" not in over and over.get("status") != "blocked":
        acc = await account(rows)
        over |= {
            "tenant_id": acc["tenant_id"],
            "merchant_id": acc["merchant_id"],
            "environment": acc["environment"],
            "issuer_account_id": acc["id"],
        }
    return await rows.document(**over)


def _now() -> datetime:
    return datetime.now(UTC)


async def test_original_queued_row_accepted(rows: Rows) -> None:
    acc = await account(rows)
    await original(
        rows,
        tenant_id=acc["tenant_id"],
        merchant_id=acc["merchant_id"],
        environment="test",
        issuer_account_id=acc["id"],
        status="queued",
        queued_at=_now(),
        buyer_source="host",
        document={"buyer": {}},
        total_vnd=100,
    )


async def test_original_needs_draft_row_accepted(rows: Rows) -> None:
    await original(rows, status="needs_draft")


async def test_original_blocked_without_issuer_accepted(rows: Rows) -> None:
    row = rows.document_row(status="blocked", issuer_account_id=None)
    await rows.insert(rows.tables.documents, row)


async def test_child_with_null_issuer_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment=parent["environment"],
            issuer_account_id=None,
            kind="adjustment",
            original_document_id=parent["id"],
            parent_correction_mode="adjust",
            adjustment_type="increase",
            status="blocked",
        ),
    )


async def test_issuer_account_one_per_merchant_environment(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "issuer_accounts_scope_uq",
        rows.tables.issuer_accounts,
        **rows.issuer_account_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment=acc["environment"],
        ),
    )


async def test_live_active_requires_sandbox_evidence(rows: Rows) -> None:
    await rows.expect_violation(
        "issuer_accounts_live_evidence_ck",
        rows.tables.issuer_accounts,
        **rows.issuer_account_row(
            environment="live",
            status="active",
            issue_date_cross_day_action="review",
            sandbox_evidence_ref=None,
        ),
    )


async def test_active_requires_cross_day_action(rows: Rows) -> None:
    await rows.expect_violation(
        "issuer_accounts_active_config_ck",
        rows.tables.issuer_accounts,
        **rows.issuer_account_row(status="active", issue_date_cross_day_action=None),
    )


async def test_credential_ref_must_use_env_scheme(rows: Rows) -> None:
    await rows.expect_violation(
        "issuer_accounts_credential_ref_ck",
        rows.tables.issuer_accounts,
        **rows.issuer_account_row(credential_ref="plain-text"),
    )


async def test_fallback_strings_must_not_be_blank(rows: Rows) -> None:
    await rows.expect_violation(
        "issuer_accounts_fallback_ck",
        rows.tables.issuer_accounts,
        **rows.issuer_account_row(buyer_fallback_name="   "),
    )


async def test_draft_declared_must_equal_computed(rows: Rows) -> None:
    await rows.expect_violation(
        "drafts_total_match_ck",
        rows.tables.invoice_drafts,
        **rows.draft_row(declared_total_vnd=101),
    )


async def test_draft_total_must_be_positive(rows: Rows) -> None:
    await rows.expect_violation(
        "drafts_total_positive_ck",
        rows.tables.invoice_drafts,
        **rows.draft_row(declared_total_vnd=0, computed_total_vnd=0),
    )


async def test_draft_host_ref_unique(rows: Rows) -> None:
    first = await rows.draft()
    await rows.expect_violation(
        "drafts_host_ref_uq",
        rows.tables.invoice_drafts,
        **rows.draft_row(
            tenant_id=first["tenant_id"],
            host_ref_type=first["host_ref_type"],
            host_ref_id=first["host_ref_id"],
        ),
    )


async def test_draft_intent_unique(rows: Rows) -> None:
    intent = rows._id()
    await rows.draft(intent_id=intent)
    await rows.expect_violation(
        "drafts_intent_uq", rows.tables.invoice_drafts, **rows.draft_row(intent_id=intent)
    )


async def test_draft_consumed_requires_document(rows: Rows) -> None:
    await rows.expect_violation(
        "drafts_consumed_ck",
        rows.tables.invoice_drafts,
        **rows.draft_row(status="consumed", consumed_by_document_id=None),
    )


async def test_document_issuer_of_other_merchant_rejected(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_issuer_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=rows._id(),
            environment=acc["environment"],
            issuer_account_id=acc["id"],
        ),
    )


async def test_document_draft_of_other_merchant_rejected(rows: Rows) -> None:
    acc = await account(rows)
    draft = await rows.draft(tenant_id=acc["tenant_id"], merchant_id=acc["merchant_id"])
    other = await account(rows, tenant_id=acc["tenant_id"])
    await rows.expect_violation(
        "documents_draft_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=other["tenant_id"],
            merchant_id=other["merchant_id"],
            environment=other["environment"],
            issuer_account_id=other["id"],
            draft_id=draft["id"],
        ),
    )


async def test_two_originals_same_settlement_rejected(rows: Rows) -> None:
    first = await original(rows)
    await rows.expect_violation(
        "documents_original_settlement_uq",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=first["tenant_id"],
            merchant_id=first["merchant_id"],
            environment=first["environment"],
            issuer_account_id=first["issuer_account_id"],
            settlement_id=first["settlement_id"],
        ),
    )


async def test_adjustment_same_settlement_allowed(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    child = rows.document_row(
        tenant_id=parent["tenant_id"],
        merchant_id=parent["merchant_id"],
        environment=parent["environment"],
        issuer_account_id=parent["issuer_account_id"],
        kind="adjustment",
        settlement_id=parent["settlement_id"],
        settled_amount_vnd=None,
        settled_at=None,
        original_document_id=parent["id"],
        parent_correction_mode="adjust",
        adjustment_type="increase",
    )
    await rows.insert(rows.tables.documents, child)


async def test_valid_receipt_accepted(rows: Rows) -> None:
    document = await original(rows)
    await rows.receipt(
        tenant_id=document["tenant_id"],
        merchant_id=document["merchant_id"],
        environment=document["environment"],
        settlement_id=document["settlement_id"],
        amount_vnd=document["settled_amount_vnd"],
        document_id=document["id"],
    )


async def test_same_scope_draft_id_accepted(rows: Rows) -> None:
    acc = await account(rows)
    draft = await rows.draft(tenant_id=acc["tenant_id"], merchant_id=acc["merchant_id"])
    await rows.document(
        tenant_id=acc["tenant_id"],
        merchant_id=acc["merchant_id"],
        environment=acc["environment"],
        issuer_account_id=acc["id"],
        draft_id=draft["id"],
    )


async def test_reference_code_unique_per_issuer_account(rows: Rows) -> None:
    first = await original(rows)
    await rows.expect_violation(
        "documents_reference_uq",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=first["tenant_id"],
            merchant_id=first["merchant_id"],
            environment=first["environment"],
            issuer_account_id=first["issuer_account_id"],
            reference_code=first["reference_code"],
        ),
    )


async def test_original_total_must_equal_settled_amount(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_original_total_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="queued",
            queued_at=_now(),
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=101,
            settled_amount_vnd=100,
        ),
    )


async def test_queued_requires_frozen_snapshot_and_queued_at(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_frozen_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="queued",
            queued_at=_now(),
        ),
    )
    await rows.expect_violation(
        "documents_queued_at_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="queued",
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=100,
            queued_at=None,
        ),
    )


async def test_pending_requires_frozen_request_and_issued_date(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_request_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="pending_provider",
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=100,
            pending_since=_now(),
        ),
    )


async def test_issued_requires_invoice_number_and_tax_code(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_issued_ids_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="issued",
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=100,
        ),
    )


async def test_pending_requires_pending_since(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_pending_since_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="pending_provider",
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=100,
            request={},
            request_frozen_at=_now(),
            issued_date=_now().date(),
            pending_since=None,
        ),
    )


async def test_issuer_null_only_when_blocked(rows: Rows) -> None:
    await rows.expect_violation(
        "documents_issuer_required_ck",
        rows.tables.documents,
        **rows.document_row(issuer_account_id=None, status="needs_draft"),
    )


async def test_child_with_null_parent_mode_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment=parent["environment"],
            issuer_account_id=parent["issuer_account_id"],
            kind="adjustment",
            original_document_id=parent["id"],
            parent_correction_mode=None,
            adjustment_type="increase",
        ),
    )


async def test_child_with_missing_parent_rejected(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_parent_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            kind="adjustment",
            original_document_id=rows._id(),
            parent_correction_mode="adjust",
            adjustment_type="increase",
        ),
    )


async def test_child_under_other_merchant_parent_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    child_account = await account(rows, tenant_id=parent["tenant_id"])
    await rows.expect_violation(
        "documents_parent_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=child_account["tenant_id"],
            merchant_id=child_account["merchant_id"],
            environment=child_account["environment"],
            issuer_account_id=child_account["id"],
            kind="adjustment",
            original_document_id=parent["id"],
            parent_correction_mode="adjust",
            adjustment_type="increase",
        ),
    )


async def test_child_under_other_environment_parent_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    child_account = await account(
        rows, tenant_id=parent["tenant_id"], merchant_id=parent["merchant_id"], environment="live"
    )
    await rows.expect_violation(
        "documents_parent_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment="live",
            issuer_account_id=child_account["id"],
            kind="adjustment",
            original_document_id=parent["id"],
            parent_correction_mode="adjust",
            adjustment_type="increase",
        ),
    )


async def test_child_under_other_issuer_parent_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    # Same-scope alternate issuers cannot exist because the unique scope constraint allows one.
    other = await account(rows, tenant_id="tenant-other")
    await rows.expect_violation(
        "documents_parent_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=other["tenant_id"],
            merchant_id=other["merchant_id"],
            environment=other["environment"],
            issuer_account_id=other["id"],
            kind="adjustment",
            original_document_id=parent["id"],
            parent_correction_mode="adjust",
            adjustment_type="increase",
        ),
    )


async def test_replacement_under_adjust_mode_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    await rows.expect_violation(
        "documents_parent_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment=parent["environment"],
            issuer_account_id=parent["issuer_account_id"],
            kind="replacement",
            original_document_id=parent["id"],
            parent_correction_mode="replace",
        ),
    )


async def test_adjustment_under_replace_mode_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="replace")
    await rows.expect_violation(
        "documents_parent_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment=parent["environment"],
            issuer_account_id=parent["issuer_account_id"],
            kind="adjustment",
            original_document_id=parent["id"],
            parent_correction_mode="adjust",
            adjustment_type="increase",
        ),
    )


async def test_child_under_unlocked_parent_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode=None)
    await rows.expect_violation(
        "documents_parent_fk",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment=parent["environment"],
            issuer_account_id=parent["issuer_account_id"],
            kind="replacement",
            original_document_id=parent["id"],
            parent_correction_mode="replace",
        ),
    )


async def test_correction_mode_locked_once_child_exists(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    child = rows.document_row(
        tenant_id=parent["tenant_id"],
        merchant_id=parent["merchant_id"],
        environment=parent["environment"],
        issuer_account_id=parent["issuer_account_id"],
        kind="adjustment",
        original_document_id=parent["id"],
        parent_correction_mode="adjust",
        adjustment_type="increase",
        settled_amount_vnd=None,
        settled_at=None,
    )
    await rows.insert(rows.tables.documents, child)
    with pytest.raises(IntegrityError) as caught:
        await rows.conn.execute(
            rows.tables.documents.update()
            .where(rows.tables.documents.c.id == parent["id"])
            .values(correction_mode="replace")
        )
    assert "documents_parent_fk" in str(caught.value)


async def test_receipt_document_of_other_scope_rejected(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "settlement_receipts_document_fk",
        rows.tables.settlement_receipts,
        **rows.receipt_row(
            tenant_id=doc["tenant_id"],
            merchant_id=rows._id(),
            environment=doc["environment"],
            settlement_id=doc["settlement_id"],
            amount_vnd=doc["settled_amount_vnd"],
            document_id=doc["id"],
        ),
    )


async def test_receipt_amount_differs_from_document_rejected(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "settlement_receipts_document_fk",
        rows.tables.settlement_receipts,
        **rows.receipt_row(
            tenant_id=doc["tenant_id"],
            merchant_id=doc["merchant_id"],
            environment=doc["environment"],
            settlement_id=doc["settlement_id"],
            amount_vnd=101,
            document_id=doc["id"],
        ),
    )


async def test_receipt_settlement_differs_from_document_rejected(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "settlement_receipts_document_fk",
        rows.tables.settlement_receipts,
        **rows.receipt_row(
            tenant_id=doc["tenant_id"],
            merchant_id=doc["merchant_id"],
            environment=doc["environment"],
            settlement_id=rows._id(),
            amount_vnd=doc["settled_amount_vnd"],
            document_id=doc["id"],
        ),
    )


async def test_one_open_review_per_document(rows: Rows) -> None:
    doc = await original(rows)
    await rows.review(tenant_id=doc["tenant_id"], document_id=doc["id"])
    await rows.expect_violation(
        "review_cases_one_open_uq",
        rows.tables.review_cases,
        **rows.review_row(tenant_id=doc["tenant_id"], document_id=doc["id"]),
    )


async def test_resolved_review_requires_actor_and_time(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "review_cases_resolved_ck",
        rows.tables.review_cases,
        **rows.review_row(
            tenant_id=doc["tenant_id"],
            document_id=doc["id"],
            status="resolved",
            resolution="retry",
            resolved_by=None,
            resolved_at=None,
        ),
    )


async def test_resolved_review_rejects_blank_actor(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "review_cases_resolved_ck",
        rows.tables.review_cases,
        **rows.review_row(
            tenant_id=doc["tenant_id"],
            document_id=doc["id"],
            status="resolved",
            resolution="retry",
            resolved_by="  ",
            resolved_at=_now(),
        ),
    )


async def _assert_waive_note_rejected(rows: Rows, note: str | None) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "review_cases_note_ck",
        rows.tables.review_cases,
        **rows.review_row(
            tenant_id=doc["tenant_id"],
            document_id=doc["id"],
            status="resolved",
            resolution="waive",
            resolved_by="operator",
            resolved_at=_now(),
            resolution_note=note,
        ),
    )


async def test_waive_rejects_null_note(rows: Rows) -> None:
    await _assert_waive_note_rejected(rows, None)


async def test_waive_rejects_empty_note(rows: Rows) -> None:
    await _assert_waive_note_rejected(rows, "")


async def test_waive_rejects_whitespace_note(rows: Rows) -> None:
    await _assert_waive_note_rejected(rows, "   ")


async def test_acknowledge_requires_note(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "review_cases_note_ck",
        rows.tables.review_cases,
        **rows.review_row(
            tenant_id=doc["tenant_id"],
            document_id=doc["id"],
            reason="DUPLICATE_SETTLEMENT",
            status="resolved",
            resolution="acknowledge",
            resolved_by="operator",
            resolved_at=_now(),
        ),
    )


async def test_acknowledge_only_for_allowed_reasons(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "review_cases_acknowledge_ck",
        rows.tables.review_cases,
        **rows.review_row(
            tenant_id=doc["tenant_id"],
            document_id=doc["id"],
            reason="DRAFT_MISSING",
            status="resolved",
            resolution="acknowledge",
            resolved_by="operator",
            resolved_at=_now(),
            resolution_note="checked",
        ),
    )


async def test_acknowledge_stale_reference_allowed(rows: Rows) -> None:
    doc = await original(rows)
    await rows.review(
        tenant_id=doc["tenant_id"],
        document_id=doc["id"],
        reason="STALE_REFERENCE_FOUND",
        status="resolved",
        resolution="acknowledge",
        resolved_by="operator",
        resolved_at=_now(),
        resolution_note="checked",
    )


async def test_retry_with_new_reference_rejects_blank_note(rows: Rows) -> None:
    doc = await original(rows)
    await rows.expect_violation(
        "review_cases_note_ck",
        rows.tables.review_cases,
        **rows.review_row(
            tenant_id=doc["tenant_id"],
            document_id=doc["id"],
            status="resolved",
            resolution="retry_with_new_reference",
            resolved_by="operator",
            resolved_at=_now(),
            resolution_note=" ",
        ),
    )


async def test_cancelled_externally_requires_audit(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_cancelled_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="cancelled_externally",
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=100,
        ),
    )


async def test_unknown_status_rejected(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_status_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment="test",
            issuer_account_id=acc["id"],
            status="unknown",
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=100,
        ),
    )


async def test_child_with_null_original_document_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="replace")
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment=parent["environment"],
            issuer_account_id=parent["issuer_account_id"],
            kind="replacement",
            original_document_id=None,
            parent_correction_mode="replace",
        ),
    )
