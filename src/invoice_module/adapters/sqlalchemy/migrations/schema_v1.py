"""Frozen literal DDL for invoice-module schema version 1.

Schema version 1 is frozen from release 0.1.0. Later changes belong in schema_v2.
"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA_VERSION = 1
TABLE_COUNT = 5


def _json() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def _name(prefix: str, name: str) -> str:
    return f"{prefix}{name}"


def upgrade(op: Any, prefix: str = "inv_") -> None:
    """Create the frozen v1 schema."""

    op.create_table(
        _name(prefix, "invoice_drafts"),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("host_ref_type", sa.String(64), nullable=False),
        sa.Column("host_ref_id", sa.String(255), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=True),
        sa.Column("buyer", _json(), nullable=True),
        sa.Column("items", _json(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("declared_total_vnd", sa.BigInteger(), nullable=False),
        sa.Column("computed_total_vnd", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("consumed_by_document_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "(status = 'consumed') = (consumed_by_document_id IS NOT NULL)",
            name=_name(prefix, "drafts_consumed_ck"),
        ),
        sa.CheckConstraint("revision >= 1", name=_name(prefix, "drafts_revision_ck")),
        sa.CheckConstraint(
            "status IN ('consumed', 'discarded', 'open')", name=_name(prefix, "drafts_status_ck")
        ),
        sa.CheckConstraint(
            "declared_total_vnd = computed_total_vnd", name=_name(prefix, "drafts_total_match_ck")
        ),
        sa.CheckConstraint(
            "declared_total_vnd > 0", name=_name(prefix, "drafts_total_positive_ck")
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "id", name=_name(prefix, "drafts_fk_target_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id", "host_ref_type", "host_ref_id", name=_name(prefix, "drafts_host_ref_uq")
        ),
        sa.UniqueConstraint("tenant_id", "intent_id", name=_name(prefix, "drafts_intent_uq")),
    )

    op.create_table(
        _name(prefix, "issuer_accounts"),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_account_id", sa.String(64), nullable=False),
        sa.Column("template_code", sa.String(8), nullable=False),
        sa.Column("invoice_series_template", sa.String(32), nullable=False),
        sa.Column("payment_method", sa.String(32), nullable=False),
        sa.Column("credential_ref", sa.String(128), nullable=False),
        sa.Column("seller_store_xid", sa.String(64), nullable=True),
        sa.Column("buyer_fallback_name", sa.Text(), nullable=False),
        sa.Column("buyer_fallback_address", sa.Text(), nullable=False),
        sa.Column("buyer_fallback_extra", _json(), nullable=True),
        sa.Column("issue_date_cross_day_action", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sandbox_evidence_ref", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status <> 'active' OR issue_date_cross_day_action IS NOT NULL",
            name=_name(prefix, "issuer_accounts_active_config_ck"),
        ),
        sa.CheckConstraint(
            "credential_ref LIKE 'env:%'", name=_name(prefix, "issuer_accounts_credential_ref_ck")
        ),
        sa.CheckConstraint(
            "issue_date_cross_day_action IS NULL OR issue_date_cross_day_action IN ('issue', 'review')",  # noqa: E501
            name=_name(prefix, "issuer_accounts_cross_day_ck"),
        ),
        sa.CheckConstraint(
            "environment IN ('live', 'test')", name=_name(prefix, "issuer_accounts_environment_ck")
        ),
        sa.CheckConstraint(
            "length(btrim(buyer_fallback_name)) > 0 AND length(btrim(buyer_fallback_address)) > 0",
            name=_name(prefix, "issuer_accounts_fallback_ck"),
        ),
        sa.CheckConstraint(
            "environment = 'test' OR status <> 'active' OR sandbox_evidence_ref IS NOT NULL",
            name=_name(prefix, "issuer_accounts_live_evidence_ck"),
        ),
        sa.CheckConstraint(
            "provider = 'sepay_einvoice'", name=_name(prefix, "issuer_accounts_provider_ck")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'draft')",
            name=_name(prefix, "issuer_accounts_status_ck"),
        ),
        sa.CheckConstraint(
            "template_code IN ('1', '2')", name=_name(prefix, "issuer_accounts_template_ck")
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "environment",
            "id",
            name=_name(prefix, "issuer_accounts_fk_target_uq"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "environment",
            name=_name(prefix, "issuer_accounts_scope_uq"),
        ),
    )

    op.create_table(
        _name(prefix, "documents"),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("issuer_account_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("host_ref_type", sa.String(64), nullable=False),
        sa.Column("host_ref_id", sa.String(255), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=True),
        sa.Column("original_document_id", sa.Uuid(), nullable=True),
        sa.Column("parent_correction_mode", sa.String(8), nullable=True),
        sa.Column("correction_mode", sa.String(8), nullable=True),
        sa.Column("adjustment_type", sa.String(16), nullable=True),
        sa.Column("reference_code", sa.String(64), nullable=False),
        sa.Column("document", _json(), nullable=True),
        sa.Column("request", _json(), nullable=True),
        sa.Column("request_frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operator_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "request_authorized_resend", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("buyer_source", sa.String(16), nullable=True),
        sa.Column("total_vnd", sa.BigInteger(), nullable=True),
        sa.Column("settled_amount_vnd", sa.BigInteger(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tracking_code", sa.String(128), nullable=True),
        sa.Column("provider_invoice_id", sa.String(128), nullable=True),
        sa.Column("invoice_number", sa.String(32), nullable=True),
        sa.Column("tax_authority_code", sa.String(64), nullable=True),
        sa.Column("issued_date", sa.Date(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pdf_uri", sa.Text(), nullable=True),
        sa.Column("xml_uri", sa.Text(), nullable=True),
        sa.Column("pdf_sha256", sa.String(64), nullable=True),
        sa.Column("xml_sha256", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(48), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.String(128), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "adjustment_type IS NULL OR adjustment_type IN ('combined', 'decrease', 'increase', 'info')",  # noqa: E501
            name=_name(prefix, "documents_adjustment_type_ck"),
        ),
        sa.CheckConstraint(
            "buyer_source IS NULL OR buyer_source IN ('fallback', 'host')",
            name=_name(prefix, "documents_buyer_source_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'cancelled_externally' OR (cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND cancel_reason IS NOT NULL)",  # noqa: E501
            name=_name(prefix, "documents_cancelled_ck"),
        ),
        sa.CheckConstraint(
            "correction_mode IS NULL OR correction_mode IN ('adjust', 'replace')",
            name=_name(prefix, "documents_correction_mode_ck"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND poll_count >= 0 AND lease_generation >= 0 AND request_revision >= 0",
            name=_name(prefix, "documents_counters_ck"),
        ),
        sa.CheckConstraint(
            "environment IN ('live', 'test')", name=_name(prefix, "documents_environment_ck")
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code IN ('artifact_fetch_failed', 'auth_failed', 'correction_rejected', 'document_existed_unresolved', 'issued_date_invalid', 'lookup_mismatch', 'max_attempts_exceeded', 'provider_failed', 'provider_timeout', 'quota_exhausted', 'registration_not_approved', 'seller_store_invalid', 'transient_error', 'validation_error')",  # noqa: E501
            name=_name(prefix, "documents_error_code_ck"),
        ),
        sa.CheckConstraint(
            "status IN ('needs_draft', 'blocked') OR (document IS NOT NULL AND total_vnd IS NOT NULL AND buyer_source IS NOT NULL)",  # noqa: E501
            name=_name(prefix, "documents_frozen_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'issued' OR (invoice_number IS NOT NULL AND tax_authority_code IS NOT NULL)",
            name=_name(prefix, "documents_issued_ids_ck"),
        ),
        sa.CheckConstraint(
            "issuer_account_id IS NOT NULL OR status = 'blocked'",
            name=_name(prefix, "documents_issuer_required_ck"),
        ),
        sa.CheckConstraint(
            "kind IN ('adjustment', 'original', 'replacement')",
            name=_name(prefix, "documents_kind_ck"),
        ),
        sa.CheckConstraint(
            "(kind = 'original' AND original_document_id IS NULL AND parent_correction_mode IS NULL AND adjustment_type IS NULL AND settled_amount_vnd IS NOT NULL AND settled_at IS NOT NULL) OR (kind = 'adjustment' AND tenant_id IS NOT NULL AND merchant_id IS NOT NULL AND environment IS NOT NULL AND issuer_account_id IS NOT NULL AND original_document_id IS NOT NULL AND parent_correction_mode IS NOT NULL AND parent_correction_mode = 'adjust' AND adjustment_type IS NOT NULL) OR (kind = 'replacement' AND tenant_id IS NOT NULL AND merchant_id IS NOT NULL AND environment IS NOT NULL AND issuer_account_id IS NOT NULL AND original_document_id IS NOT NULL AND parent_correction_mode IS NOT NULL AND parent_correction_mode = 'replace' AND adjustment_type IS NULL)",  # noqa: E501
            name=_name(prefix, "documents_kind_shape_ck"),
        ),
        sa.CheckConstraint(
            "kind <> 'original' OR total_vnd IS NULL OR total_vnd = settled_amount_vnd",
            name=_name(prefix, "documents_original_total_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'pending_provider' OR pending_since IS NOT NULL",
            name=_name(prefix, "documents_pending_since_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'queued' OR queued_at IS NOT NULL",
            name=_name(prefix, "documents_queued_at_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'pending_provider' OR (request IS NOT NULL AND request_frozen_at IS NOT NULL AND issued_date IS NOT NULL)",  # noqa: E501
            name=_name(prefix, "documents_request_ck"),
        ),
        sa.CheckConstraint(
            "status IN ('blocked', 'cancelled_externally', 'failed', 'issued', 'needs_draft', 'pending_provider', 'queued', 'replaced')",  # noqa: E501
            name=_name(prefix, "documents_status_ck"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "draft_id"],
            [
                f"{prefix}invoice_drafts.tenant_id",
                f"{prefix}invoice_drafts.merchant_id",
                f"{prefix}invoice_drafts.id",
            ],
            name=_name(prefix, "documents_draft_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "environment", "issuer_account_id"],
            [
                f"{prefix}issuer_accounts.tenant_id",
                f"{prefix}issuer_accounts.merchant_id",
                f"{prefix}issuer_accounts.environment",
                f"{prefix}issuer_accounts.id",
            ],
            name=_name(prefix, "documents_issuer_fk"),
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "merchant_id",
                "environment",
                "issuer_account_id",
                "original_document_id",
                "parent_correction_mode",
            ],
            [
                f"{prefix}documents.tenant_id",
                f"{prefix}documents.merchant_id",
                f"{prefix}documents.environment",
                f"{prefix}documents.issuer_account_id",
                f"{prefix}documents.id",
                f"{prefix}documents.correction_mode",
            ],
            name=_name(prefix, "documents_parent_fk"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "environment",
            "issuer_account_id",
            "id",
            "correction_mode",
            name=_name(prefix, "documents_correction_target_uq"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "environment",
            "settlement_id",
            "settled_amount_vnd",
            "id",
            name=_name(prefix, "documents_receipt_target_uq"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "environment",
            "id",
            name=_name(prefix, "documents_scope_target_uq"),
        ),
        sa.UniqueConstraint("tenant_id", "id", name=_name(prefix, "documents_tenant_target_uq")),
    )

    op.create_table(
        _name(prefix, "review_cases"),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("details", _json(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=True),
        sa.Column("resolved_by", sa.String(128), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution <> 'acknowledge' OR reason IN ('DUPLICATE_SETTLEMENT', 'QUOTA_EXHAUSTED', 'STALE_REFERENCE_FOUND')",  # noqa: E501
            name=_name(prefix, "review_cases_acknowledge_ck"),
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution NOT IN ('acknowledge', 'retry_with_new_reference', 'waive') OR (resolution_note IS NOT NULL AND length(btrim(resolution_note)) > 0)",  # noqa: E501
            name=_name(prefix, "review_cases_note_ck"),
        ),
        sa.CheckConstraint(
            "reason IN ('AMOUNT_MISMATCH', 'AUTH_FAILED', 'CORRECTION_REJECTED', 'DOCUMENT_EXISTED_UNRESOLVED', 'DRAFT_MISSING', 'DUPLICATE_SETTLEMENT', 'ISSUER_ACCOUNT_UNAVAILABLE', 'ISSUE_DATE_CROSSED_DAY', 'LOOKUP_MISMATCH', 'MAX_ATTEMPTS_EXCEEDED', 'PROVIDER_FAILED', 'PROVIDER_REJECTED', 'PROVIDER_TIMEOUT', 'QUOTA_EXHAUSTED', 'STALE_REFERENCE_FOUND')",  # noqa: E501
            name=_name(prefix, "review_cases_reason_ck"),
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('acknowledge', 'attach_draft', 'mark_external', 'retry', 'retry_with_new_reference', 'waive')",  # noqa: E501
            name=_name(prefix, "review_cases_resolution_ck"),
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolution IS NULL AND resolved_by IS NULL AND resolved_at IS NULL) OR (status = 'resolved' AND resolution IS NOT NULL AND resolved_by IS NOT NULL AND length(btrim(resolved_by)) > 0 AND resolved_at IS NOT NULL)",  # noqa: E501
            name=_name(prefix, "review_cases_resolved_ck"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name=_name(prefix, "review_cases_status_ck")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            [f"{prefix}documents.tenant_id", f"{prefix}documents.id"],
            name=_name(prefix, "review_cases_document_fk"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        _name(prefix, "settlement_receipts"),
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("host_ref_type", sa.String(64), nullable=False),
        sa.Column("host_ref_id", sa.String(255), nullable=False),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("amount_vnd > 0", name=_name(prefix, "settlement_receipts_amount_ck")),
        sa.CheckConstraint(
            "environment IN ('live', 'test')",
            name=_name(prefix, "settlement_receipts_environment_ck"),
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "merchant_id",
                "environment",
                "settlement_id",
                "amount_vnd",
                "document_id",
            ],
            [
                f"{prefix}documents.tenant_id",
                f"{prefix}documents.merchant_id",
                f"{prefix}documents.environment",
                f"{prefix}documents.settlement_id",
                f"{prefix}documents.settled_amount_vnd",
                f"{prefix}documents.id",
            ],
            name=_name(prefix, "settlement_receipts_document_fk"),
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )

    op.create_index(
        _name(prefix, "documents_due_ix"),
        _name(prefix, "documents"),
        ["status", "next_attempt_at"],
        unique=False,
    )

    op.create_index(
        _name(prefix, "documents_host_ref_ix"),
        _name(prefix, "documents"),
        ["tenant_id", "host_ref_type", "host_ref_id"],
        unique=False,
    )

    op.create_index(
        _name(prefix, "documents_original_settlement_uq"),
        _name(prefix, "documents"),
        ["tenant_id", "settlement_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'original'"),
    )

    op.create_index(
        _name(prefix, "documents_reference_uq"),
        _name(prefix, "documents"),
        ["issuer_account_id", "reference_code"],
        unique=True,
    )

    op.create_index(
        _name(prefix, "settlement_receipts_settlement_ix"),
        _name(prefix, "settlement_receipts"),
        ["tenant_id", "settlement_id"],
        unique=False,
    )

    op.create_index(
        _name(prefix, "review_cases_one_open_uq"),
        _name(prefix, "review_cases"),
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_index(
        _name(prefix, "review_cases_open_ix"),
        _name(prefix, "review_cases"),
        ["status", "opened_at"],
        unique=False,
    )


def downgrade(op: Any, prefix: str = "inv_") -> None:
    """Drop the frozen v1 schema."""
    op.drop_index(
        _name(prefix, "settlement_receipts_settlement_ix"),
        table_name=_name(prefix, "settlement_receipts"),
    )
    op.drop_table(_name(prefix, "settlement_receipts"))
    op.drop_index(_name(prefix, "review_cases_open_ix"), table_name=_name(prefix, "review_cases"))
    op.drop_index(
        _name(prefix, "review_cases_one_open_uq"), table_name=_name(prefix, "review_cases")
    )
    op.drop_table(_name(prefix, "review_cases"))
    op.drop_index(_name(prefix, "documents_reference_uq"), table_name=_name(prefix, "documents"))
    op.drop_index(
        _name(prefix, "documents_original_settlement_uq"), table_name=_name(prefix, "documents")
    )
    op.drop_index(_name(prefix, "documents_host_ref_ix"), table_name=_name(prefix, "documents"))
    op.drop_index(_name(prefix, "documents_due_ix"), table_name=_name(prefix, "documents"))
    op.drop_table(_name(prefix, "documents"))
    op.drop_table(_name(prefix, "issuer_accounts"))
    op.drop_table(_name(prefix, "invoice_drafts"))
