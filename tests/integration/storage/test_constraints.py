from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
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


async def test_blank_fallback_address_accepted(rows: Rows) -> None:
    acc = await account(
        rows, status="active", issue_date_cross_day_action="issue", buyer_fallback_address=""
    )
    table = rows.tables.issuer_accounts
    stored = await rows.conn.scalar(
        sa.select(table.c.buyer_fallback_address).where(table.c.id == acc["id"])
    )
    assert stored == ""


async def test_fallback_name_still_rejected_when_address_blank(rows: Rows) -> None:
    await rows.expect_violation(
        "issuer_accounts_fallback_ck",
        rows.tables.issuer_accounts,
        **rows.issuer_account_row(buyer_fallback_name="   ", buyer_fallback_address=""),
    )


async def test_null_fallback_address_rejected(rows: Rows) -> None:
    with pytest.raises(IntegrityError) as caught:
        await rows.insert(
            rows.tables.issuer_accounts, rows.issuer_account_row(buyer_fallback_address=None)
        )
    cause = caught.value.orig.__cause__
    assert (cause.sqlstate, cause.column_name) == ("23502", "buyer_fallback_address")


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


@pytest.mark.parametrize("environment", ["test", "live"])
async def test_issued_requires_invoice_number(rows: Rows, environment: str) -> None:
    acc = await account(rows, environment=environment)
    await rows.expect_violation(
        "documents_issued_ids_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment=environment,
            issuer_account_id=acc["id"],
            status="issued",
            document={"buyer": {}},
            buyer_source="host",
            total_vnd=100,
            invoice_number=None,
            tax_authority_code="TAX-CODE",
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


async def test_replacement_with_adjust_parent_mode_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="adjust")
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=parent["tenant_id"],
            merchant_id=parent["merchant_id"],
            environment=parent["environment"],
            issuer_account_id=parent["issuer_account_id"],
            kind="replacement",
            original_document_id=parent["id"],
            parent_correction_mode="adjust",
        ),
    )


async def test_adjustment_with_replace_parent_mode_rejected(rows: Rows) -> None:
    parent = await original(rows, correction_mode="replace")
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
            parent_correction_mode="replace",
            adjustment_type="increase",
        ),
    )


async def test_adjustment_requires_adjustment_type(rows: Rows) -> None:
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
            parent_correction_mode="adjust",
            adjustment_type=None,
        ),
    )


async def test_replacement_rejects_adjustment_type(rows: Rows) -> None:
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
            original_document_id=parent["id"],
            parent_correction_mode="replace",
            adjustment_type="increase",
        ),
    )


async def test_original_requires_settled_amount(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment=acc["environment"],
            issuer_account_id=acc["id"],
            kind="original",
            settled_amount_vnd=None,
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


async def test_receipt_amount_must_be_positive(rows: Rows) -> None:
    # The document cannot carry 0 any more; CHECK constraints fire before the FK trigger.
    document = await original(rows)
    await rows.expect_violation(
        "settlement_receipts_amount_ck",
        rows.tables.settlement_receipts,
        **rows.receipt_row(
            tenant_id=document["tenant_id"],
            merchant_id=document["merchant_id"],
            environment=document["environment"],
            settlement_id=document["settlement_id"],
            amount_vnd=0,
            document_id=document["id"],
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


async def test_review_case_document_must_be_same_tenant(rows: Rows) -> None:
    document = await original(rows)
    await rows.expect_violation(
        "review_cases_document_fk",
        rows.tables.review_cases,
        **rows.review_row(tenant_id="tenant-other", document_id=document["id"]),
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


async def test_adjustment_requires_original_document(rows: Rows) -> None:
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
            original_document_id=None,
            parent_correction_mode="adjust",
            adjustment_type="increase",
        ),
    )


async def test_replacement_with_null_issuer_rejected(rows: Rows) -> None:
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            kind="replacement",
            issuer_account_id=None,
            original_document_id=rows._id(),
            parent_correction_mode="replace",
            status="blocked",
        ),
    )


async def test_replacement_requires_parent_mode(rows: Rows) -> None:
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
            original_document_id=parent["id"],
            parent_correction_mode=None,
        ),
    )


async def test_original_rejects_original_document_id(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment=acc["environment"],
            issuer_account_id=acc["id"],
            original_document_id=rows._id(),
        ),
    )


async def test_original_rejects_parent_correction_mode(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment=acc["environment"],
            issuer_account_id=acc["id"],
            parent_correction_mode="adjust",
        ),
    )


async def test_original_rejects_adjustment_type(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment=acc["environment"],
            issuer_account_id=acc["id"],
            adjustment_type="increase",
        ),
    )


async def test_original_requires_settled_at(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_kind_shape_ck",
        rows.tables.documents,
        **rows.document_row(
            tenant_id=acc["tenant_id"],
            merchant_id=acc["merchant_id"],
            environment=acc["environment"],
            issuer_account_id=acc["id"],
            settled_at=None,
        ),
    )


def _frozen(acc: dict[str, object], **over: object) -> dict[str, object]:
    return {
        "tenant_id": acc["tenant_id"],
        "merchant_id": acc["merchant_id"],
        "environment": acc["environment"],
        "issuer_account_id": acc["id"],
        "document": {"buyer": {}},
        "buyer_source": "host",
        "total_vnd": 100,
    } | over


def _approval(reference_code: str, **over: object) -> dict[str, object]:
    return {
        "approved_at": _now(),
        "approved_by": "accountant-test",
        "approved_snapshot_sha256": "a" * 64,
        "approved_reference_code": reference_code,
    } | over


def _frozen_request() -> dict[str, object]:
    return {
        "request": {},
        "request_frozen_at": _now(),
        "issued_date": _now().date(),
    }


def _manual(acc: dict[str, object], **over: object) -> dict[str, object]:
    return _frozen(acc, delivery_mode="manual", reference_code="ref-manual") | over


async def _child(rows: Rows, **over: object) -> dict[str, object]:
    parent = await original(rows, correction_mode="adjust")
    return rows.document_row(
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
        **over,
    )


async def test_draft_delivery_mode_required_and_checked(rows: Rows) -> None:
    with pytest.raises(IntegrityError) as caught:
        await rows.insert(rows.tables.invoice_drafts, rows.draft_row(delivery_mode=None))
    cause = caught.value.orig.__cause__
    assert (cause.sqlstate, cause.column_name) == ("23502", "delivery_mode")
    await rows.expect_violation(
        "drafts_delivery_mode_ck",
        rows.tables.invoice_drafts,
        **rows.draft_row(delivery_mode="later"),
    )


async def test_delivery_mode_has_no_default(rows: Rows) -> None:
    draft = rows.draft_row()
    del draft["delivery_mode"]
    with pytest.raises(IntegrityError) as caught:
        await rows.insert(rows.tables.invoice_drafts, draft)
    cause = caught.value.orig.__cause__
    assert (cause.sqlstate, cause.column_name) == ("23502", "delivery_mode")
    document = rows.document_row(status="blocked", issuer_account_id=None)
    del document["delivery_mode"]
    await rows.insert(rows.tables.documents, document)
    table = rows.tables.documents
    stored = await rows.conn.scalar(
        sa.select(table.c.delivery_mode).where(table.c.id == document["id"])
    )
    assert stored is None


async def test_frozen_original_requires_delivery_mode(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_delivery_mode_frozen_ck",
        rows.tables.documents,
        **rows.document_row(**_frozen(acc, status="blocked", delivery_mode=None)),
    )


async def test_document_delivery_mode_checked(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_delivery_mode_ck",
        rows.tables.documents,
        **rows.document_row(**_frozen(acc, status="blocked", delivery_mode="later")),
    )


async def test_none_snapshot_persists_as_sql_null(rows: Rows) -> None:
    acc = await account(rows)
    row = await rows.document(
        **_frozen(acc, status="needs_draft", document=None, request=None, delivery_mode=None)
        | {"buyer_source": None, "total_vnd": None}
    )
    table = rows.tables.documents
    stored = await rows.conn.execute(
        sa.select(table.c.document.is_(None), table.c.request.is_(None)).where(
            table.c.id == row["id"]
        )
    )
    assert tuple(stored.one()) == (True, True)


@pytest.mark.parametrize(
    "missing",
    ["approved_at", "approved_by", "approved_snapshot_sha256", "approved_reference_code"],
)
async def test_approval_columns_all_or_none_and_nonblank(rows: Rows, missing: str) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_approval_ck",
        rows.tables.documents,
        **rows.document_row(
            **_manual(acc, status="blocked") | _approval("ref-manual", **{missing: None})
        ),
    )
    await rows.expect_violation(
        "documents_approval_ck",
        rows.tables.documents,
        **rows.document_row(
            **_manual(acc, status="blocked") | _approval("ref-manual", approved_by="  ")
        ),
    )


async def test_approval_with_null_delivery_mode_rejected(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_approval_ck",
        rows.tables.documents,
        **rows.document_row(
            **_manual(acc, status="blocked", delivery_mode=None) | _approval("ref-manual")
        ),
    )


async def test_approval_on_auto_rejected(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_approval_ck",
        rows.tables.documents,
        **rows.document_row(
            **_manual(acc, status="blocked", delivery_mode="auto") | _approval("ref-manual")
        ),
    )


async def test_approval_on_correction_child_rejected(rows: Rows) -> None:
    child = await _child(
        rows,
        document={"buyer": {}},
        buyer_source="host",
        total_vnd=100,
        delivery_mode="manual",
        reference_code="ref-child",
        **_approval("ref-child"),
    )
    await rows.expect_violation("documents_approval_ck", rows.tables.documents, **child)


async def test_approval_on_original_without_snapshot_rejected(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_approval_ck",
        rows.tables.documents,
        **rows.document_row(
            **_manual(acc, status="blocked", document=None, buyer_source=None, total_vnd=None)
            | _approval("ref-manual")
        ),
    )


async def test_approved_reference_must_match_reference_code(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_approved_reference_ck",
        rows.tables.documents,
        **rows.document_row(**_manual(acc, status="blocked") | _approval("ref-other")),
    )


@pytest.mark.parametrize(
    ("status", "approval", "expected"),
    [
        ("awaiting_approval", False, "documents_manual_status_ck"),
        # An approved row must carry an approval and approvals exist only on manual rows, so
        # documents_approval_ck is the first CHECK an auto `approved` row can violate.
        ("approved", True, "documents_approval_ck"),
    ],
)
async def test_auto_awaiting_or_approved_rejected(
    rows: Rows, status: str, approval: bool, expected: str
) -> None:
    acc = await account(rows)
    values = _manual(acc, status=status, delivery_mode="auto") | _frozen_request()
    if approval:
        values |= _approval("ref-manual")
    await rows.expect_violation(expected, rows.tables.documents, **rows.document_row(**values))


async def test_correction_child_awaiting_with_null_mode_rejected(rows: Rows) -> None:
    child = await _child(
        rows,
        document={"buyer": {}},
        buyer_source="host",
        total_vnd=100,
        delivery_mode=None,
        status="awaiting_approval",
        **_frozen_request(),
    )
    await rows.expect_violation("documents_manual_status_ck", rows.tables.documents, **child)


async def test_awaiting_approval_with_audit_rejected(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_awaiting_unapproved_ck",
        rows.tables.documents,
        **rows.document_row(
            **_manual(acc, status="awaiting_approval") | _frozen_request() | _approval("ref-manual")
        ),
    )


async def test_approved_status_requires_approval(rows: Rows) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_approved_status_ck",
        rows.tables.documents,
        **rows.document_row(**_manual(acc, status="approved") | _frozen_request()),
    )


_CANCEL_AUDIT = {"cancelled_by": "operator-test", "cancel_reason": "cancelled at portal"}


@pytest.mark.parametrize("status", ["issued", "replaced", "cancelled_externally"])
async def test_manual_issued_requires_approval(rows: Rows, status: str) -> None:
    acc = await account(rows)
    values = _manual(acc, status=status, invoice_number="0000001", tax_authority_code="TAX")
    if status == "cancelled_externally":
        values |= _CANCEL_AUDIT | {"cancelled_at": _now()}
    await rows.expect_violation(
        "documents_manual_issue_ck", rows.tables.documents, **rows.document_row(**values)
    )


@pytest.mark.parametrize("status", ["awaiting_approval", "approved"])
async def test_awaiting_approval_requires_frozen_request(rows: Rows, status: str) -> None:
    acc = await account(rows)
    values = _manual(acc, status=status, issued_date=_now().date())
    if status == "approved":
        values |= _approval("ref-manual")
    await rows.expect_violation(
        "documents_request_ck", rows.tables.documents, **rows.document_row(**values)
    )


@pytest.mark.parametrize("status", ["approved", "awaiting_approval", "pending_provider"])
async def test_request_ck_rejects_null_request_with_other_markers_set(
    rows: Rows, status: str
) -> None:
    acc = await account(rows)
    if status == "pending_provider":
        values = _frozen(acc, status=status, pending_since=_now())
    else:
        values = _manual(acc, status=status)
    if status == "approved":
        values |= _approval("ref-manual")
    values |= _frozen_request() | {"request": None}
    await rows.expect_violation(
        "documents_request_ck", rows.tables.documents, **rows.document_row(**values)
    )
    # The same row with a request is accepted, so the request alone was refused.
    await rows.document(**values | {"request": {}})


@pytest.mark.parametrize("amount", [0, -1])
async def test_original_settled_amount_must_be_positive(rows: Rows, amount: int) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        "documents_settled_amount_positive_ck",
        rows.tables.documents,
        **rows.document_row(**_frozen(acc, status="needs_draft", settled_amount_vnd=amount))
        | {"document": None, "buyer_source": None, "total_vnd": None},
    )


async def test_live_environment_issued_rejects_null_tax_authority_code(rows: Rows) -> None:
    acc = await account(rows, environment="live")
    await rows.expect_violation(
        "documents_issued_ids_ck",
        rows.tables.documents,
        **rows.document_row(
            **_frozen(acc, status="issued", invoice_number="0000001", tax_authority_code=None)
        ),
    )


@pytest.mark.parametrize("status", ["replaced", "cancelled_externally"])
@pytest.mark.parametrize(
    ("environment", "invoice_number", "tax_authority_code", "accepted"),
    [
        ("live", "0000001", None, False),
        ("test", "0000001", None, True),
        ("live", None, "TAX", False),
        ("test", None, "TAX", False),
    ],
    ids=["live-no-tax", "test-no-tax", "live-no-number", "test-no-number"],
)
async def test_replaced_and_cancelled_require_invoice_ids(
    rows: Rows,
    status: str,
    environment: str,
    invoice_number: str | None,
    tax_authority_code: str | None,
    accepted: bool,
) -> None:
    acc = await account(rows, environment=environment)
    values = _frozen(
        acc, status=status, invoice_number=invoice_number, tax_authority_code=tax_authority_code
    )
    if status == "cancelled_externally":
        values |= _CANCEL_AUDIT | {"cancelled_at": _now()}
    if accepted:
        await rows.document(**values)
    else:
        await rows.expect_violation(
            "documents_terminal_ids_ck", rows.tables.documents, **rows.document_row(**values)
        )


async def test_draft_held_reason_accepted_and_unknown_rejected(rows: Rows) -> None:
    doc = await original(rows)
    await rows.review(tenant_id=doc["tenant_id"], document_id=doc["id"], reason="DRAFT_HELD")
    other = await original(rows)
    await rows.expect_violation(
        "review_cases_reason_ck",
        rows.tables.review_cases,
        **rows.review_row(tenant_id=other["tenant_id"], document_id=other["id"], reason="HELD"),
    )


async def test_manual_awaiting_without_audit_accepted(rows: Rows) -> None:
    acc = await account(rows)
    await rows.document(**_manual(acc, status="awaiting_approval") | _frozen_request())


async def test_manual_approved_with_full_audit_accepted(rows: Rows) -> None:
    acc = await account(rows)
    await rows.document(
        **_manual(acc, status="approved") | _frozen_request() | _approval("ref-manual")
    )
    await rows.document(
        **_manual(
            acc,
            status="issued",
            reference_code="ref-manual-issued",
            invoice_number="0000001",
            tax_authority_code=None,
        )
        | _approval("ref-manual-issued")
    )


async def test_correction_child_with_null_mode_unapproved_accepted(rows: Rows) -> None:
    child = await _child(
        rows, document={"buyer": {}}, buyer_source="host", total_vnd=100, delivery_mode=None
    )
    await rows.insert(rows.tables.documents, child)


async def test_auto_issued_without_approval_allowed(rows: Rows) -> None:
    acc = await account(rows)
    await rows.document(
        **_frozen(
            acc,
            status="issued",
            delivery_mode="auto",
            invoice_number="0000001",
            tax_authority_code="TAX",
        )
    )


async def test_test_environment_issued_accepts_null_tax_authority_code(rows: Rows) -> None:
    acc = await account(rows, environment="test")
    await rows.document(
        **_frozen(acc, status="issued", invoice_number="0000001", tax_authority_code=None)
    )


_SENT = "sent"
_SEND_COLUMNS = ("send_started_at", "post_count", "post_limit", "date_resend_at")


def _send_markers(
    send_started_at: str | None, post_count: int, post_limit: int, date_resend_at: str | None
) -> dict[str, object]:
    sent_at = _now()
    return {
        "send_started_at": sent_at if send_started_at == _SENT else None,
        "post_count": post_count,
        "post_limit": post_limit,
        "date_resend_at": sent_at if date_resend_at == _SENT else None,
    }


async def _stored_send_markers(rows: Rows, document_id: object) -> tuple[object, ...]:
    table = rows.tables.documents
    result = await rows.conn.execute(
        sa.select(*(table.c[name] for name in _SEND_COLUMNS)).where(table.c.id == document_id)
    )
    return tuple(result.one())


@pytest.mark.parametrize(
    ("send_started_at", "post_count", "post_limit", "date_resend_at"),
    [
        (None, 0, 2, None),
        (_SENT, 1, 2, None),
        (_SENT, 2, 2, _SENT),
        (_SENT, 2, 3, None),
        (_SENT, 3, 3, _SENT),
        (_SENT, 3, 4, None),
        (None, 0, 1, None),
        (_SENT, 1, 1, None),
    ],
)
async def test_send_markers_valid_combinations(
    rows: Rows,
    send_started_at: str | None,
    post_count: int,
    post_limit: int,
    date_resend_at: str | None,
) -> None:
    acc = await account(rows)
    markers = _send_markers(send_started_at, post_count, post_limit, date_resend_at)
    inserted = await rows.document(**_frozen(acc, status="queued", queued_at=_now()) | markers)
    assert await _stored_send_markers(rows, inserted["id"]) == tuple(markers.values())

    fresh = await rows.document(**_frozen(acc, status="queued", queued_at=_now()))
    assert await _stored_send_markers(rows, fresh["id"]) == (None, 0, 2, None)
    table = rows.tables.documents
    async with rows.conn.begin_nested():
        await rows.conn.execute(table.update().where(table.c.id == fresh["id"]).values(**markers))
    assert await _stored_send_markers(rows, fresh["id"]) == tuple(markers.values())


@pytest.mark.parametrize("column", ["post_count", "post_limit"])
async def test_null_post_counter_rejected(rows: Rows, column: str) -> None:
    acc = await account(rows)
    values = _frozen(acc, status="queued", queued_at=_now()) | {column: None}
    with pytest.raises(IntegrityError) as caught:
        await rows.insert(rows.tables.documents, rows.document_row(**values))
    cause = caught.value.orig.__cause__
    assert (cause.sqlstate, cause.column_name) == ("23502", column)


async def _expect_send_marker_violation(
    rows: Rows, constraint: str, markers: dict[str, object]
) -> None:
    acc = await account(rows)
    await rows.expect_violation(
        constraint,
        rows.tables.documents,
        **rows.document_row(**_frozen(acc, status="queued", queued_at=_now()) | markers),
    )


@pytest.mark.parametrize(("send_started_at", "post_count"), [(None, 0), (_SENT, 1)])
async def test_date_resend_requires_second_post(
    rows: Rows, send_started_at: str | None, post_count: int
) -> None:
    await _expect_send_marker_violation(
        rows, "documents_date_resend_ck", _send_markers(send_started_at, post_count, 2, _SENT)
    )


@pytest.mark.parametrize(("send_started_at", "post_count"), [(None, 1), (_SENT, 0), (_SENT, -1)])
async def test_post_count_matches_send_started(
    rows: Rows, send_started_at: str | None, post_count: int
) -> None:
    await _expect_send_marker_violation(
        rows, "documents_post_count_ck", _send_markers(send_started_at, post_count, 2, None)
    )


@pytest.mark.parametrize(
    ("send_started_at", "post_count", "post_limit"), [(None, 0, 0), (_SENT, 3, 2)]
)
async def test_post_limit_bounds(
    rows: Rows, send_started_at: str | None, post_count: int, post_limit: int
) -> None:
    await _expect_send_marker_violation(
        rows,
        "documents_post_limit_ck",
        _send_markers(send_started_at, post_count, post_limit, None),
    )
