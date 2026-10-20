from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from invoice_module.domain.enums import (
    ACKNOWLEDGEABLE_REASONS,
    NOTE_REQUIRED_RESOLUTIONS,
    AdjustmentType,
    BuyerSource,
    CorrectionMode,
    DeliveryMode,
    DocumentKind,
    DocumentStatus,
    DraftStatus,
    Environment,
    IssueErrorCode,
    IssuerAccountStatus,
    ReviewReason,
    ReviewResolution,
    ReviewStatus,
)

TABLE_COUNT = 5


def json_type() -> sa.types.TypeEngine[Any]:
    # Python None must persist as SQL NULL: the CHECKs test `IS NULL`, which a JSON 'null' fails.
    return sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


def _values(enum: Any) -> tuple[str, ...]:
    return tuple(sorted(member.value for member in enum))


def _in(column: str, enum: Any) -> str:
    values = ", ".join("'" + value.replace("'", "''") + "'" for value in _values(enum))
    return f"{column} IN ({values})"


def _ts(name: str, *, nullable: bool = False, server_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if server_now else None,
    )


@dataclass(frozen=True)
class InvoiceTables:
    prefix: str
    issuer_accounts: sa.Table
    invoice_drafts: sa.Table
    settlement_receipts: sa.Table
    documents: sa.Table
    review_cases: sa.Table


def define_tables(metadata: sa.MetaData, prefix: str = "inv_") -> InvoiceTables:
    def n(name: str) -> str:
        return f"{prefix}{name}"

    issuer = sa.Table(
        n("issuer_accounts"),
        metadata,
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
        sa.Column("seller_store_xid", sa.String(64)),
        sa.Column("buyer_fallback_name", sa.Text(), nullable=False),
        sa.Column("buyer_fallback_address", sa.Text(), nullable=False),
        sa.Column("buyer_fallback_extra", json_type()),
        sa.Column("issue_date_cross_day_action", sa.String(16)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sandbox_evidence_ref", sa.Text()),
        _ts("created_at", server_now=True),
        _ts("updated_at", server_now=True),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "environment", name=n("issuer_accounts_scope_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "environment", "id", name=n("issuer_accounts_fk_target_uq")
        ),
        sa.CheckConstraint(
            _in("environment", Environment), name=n("issuer_accounts_environment_ck")
        ),
        sa.CheckConstraint("provider = 'sepay_einvoice'", name=n("issuer_accounts_provider_ck")),
        sa.CheckConstraint("template_code IN ('1', '2')", name=n("issuer_accounts_template_ck")),
        sa.CheckConstraint(_in("status", IssuerAccountStatus), name=n("issuer_accounts_status_ck")),
        sa.CheckConstraint(
            "issue_date_cross_day_action IS NULL OR issue_date_cross_day_action IN ('issue', 'review')",  # noqa: E501
            name=n("issuer_accounts_cross_day_ck"),
        ),
        sa.CheckConstraint(
            "credential_ref LIKE 'env:%'", name=n("issuer_accounts_credential_ref_ck")
        ),
        sa.CheckConstraint(
            "length(btrim(buyer_fallback_name)) > 0",
            name=n("issuer_accounts_fallback_ck"),
        ),
        sa.CheckConstraint(
            "environment = 'test' OR status <> 'active' OR sandbox_evidence_ref IS NOT NULL",
            name=n("issuer_accounts_live_evidence_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'active' OR issue_date_cross_day_action IS NOT NULL",
            name=n("issuer_accounts_active_config_ck"),
        ),
    )

    drafts = sa.Table(
        n("invoice_drafts"),
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("host_ref_type", sa.String(64), nullable=False),
        sa.Column("host_ref_id", sa.String(255), nullable=False),
        sa.Column("intent_id", sa.Uuid()),
        sa.Column("buyer", json_type()),
        sa.Column("items", json_type(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("delivery_mode", sa.String(8), nullable=False),
        sa.Column("declared_total_vnd", sa.BigInteger(), nullable=False),
        sa.Column("computed_total_vnd", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("consumed_by_document_id", sa.Uuid()),
        _ts("created_at", server_now=True),
        _ts("updated_at", server_now=True),
        sa.UniqueConstraint(
            "tenant_id", "host_ref_type", "host_ref_id", name=n("drafts_host_ref_uq")
        ),
        sa.UniqueConstraint("tenant_id", "intent_id", name=n("drafts_intent_uq")),
        sa.UniqueConstraint("tenant_id", "merchant_id", "id", name=n("drafts_fk_target_uq")),
        sa.CheckConstraint("declared_total_vnd > 0", name=n("drafts_total_positive_ck")),
        sa.CheckConstraint(
            "declared_total_vnd = computed_total_vnd", name=n("drafts_total_match_ck")
        ),
        sa.CheckConstraint(_in("status", DraftStatus), name=n("drafts_status_ck")),
        sa.CheckConstraint(_in("delivery_mode", DeliveryMode), name=n("drafts_delivery_mode_ck")),
        sa.CheckConstraint("revision >= 1", name=n("drafts_revision_ck")),
        sa.CheckConstraint(
            "(status = 'consumed') = (consumed_by_document_id IS NOT NULL)",
            name=n("drafts_consumed_ck"),
        ),
    )

    documents = sa.Table(
        n("documents"),
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("issuer_account_id", sa.Uuid()),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("host_ref_type", sa.String(64), nullable=False),
        sa.Column("host_ref_id", sa.String(255), nullable=False),
        sa.Column("draft_id", sa.Uuid()),
        sa.Column("original_document_id", sa.Uuid()),
        sa.Column("parent_correction_mode", sa.String(8)),
        sa.Column("correction_mode", sa.String(8)),
        sa.Column("adjustment_type", sa.String(16)),
        sa.Column("reference_code", sa.String(64), nullable=False),
        sa.Column("document", json_type()),
        sa.Column("request", json_type()),
        _ts("request_frozen_at", nullable=True),
        sa.Column("request_revision", sa.Integer(), nullable=False, server_default="0"),
        _ts("operator_retry_at", nullable=True),
        sa.Column(
            "request_authorized_resend", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("delivery_mode", sa.String(8)),
        _ts("approved_at", nullable=True),
        sa.Column("approved_by", sa.String(128)),
        sa.Column("approved_snapshot_sha256", sa.String(64)),
        sa.Column("approved_reference_code", sa.String(64)),
        sa.Column("buyer_source", sa.String(16)),
        sa.Column("total_vnd", sa.BigInteger()),
        sa.Column("settled_amount_vnd", sa.BigInteger()),
        _ts("settled_at", nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        _ts("queued_at", nullable=True),
        sa.Column("tracking_code", sa.String(128)),
        sa.Column("provider_invoice_id", sa.String(128)),
        sa.Column("invoice_number", sa.String(32)),
        sa.Column("tax_authority_code", sa.String(64)),
        sa.Column("issued_date", sa.Date()),
        _ts("issued_at", nullable=True),
        sa.Column("pdf_uri", sa.Text()),
        sa.Column("xml_uri", sa.Text()),
        sa.Column("pdf_sha256", sa.String(64)),
        sa.Column("xml_sha256", sa.String(64)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("poll_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(128)),
        _ts("lease_until", nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        _ts("next_attempt_at", nullable=True),
        _ts("pending_since", nullable=True),
        sa.Column("last_error_code", sa.String(48)),
        sa.Column("last_error_message", sa.Text()),
        _ts("cancelled_at", nullable=True),
        sa.Column("cancelled_by", sa.String(128)),
        sa.Column("cancel_reason", sa.Text()),
        sa.Column("notes", sa.Text()),
        _ts("created_at", server_now=True),
        _ts("updated_at", server_now=True),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "environment", "id", name=n("documents_scope_target_uq")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=n("documents_tenant_target_uq")),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "environment",
            "settlement_id",
            "settled_amount_vnd",
            "id",
            name=n("documents_receipt_target_uq"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "merchant_id",
            "environment",
            "issuer_account_id",
            "id",
            "correction_mode",
            name=n("documents_correction_target_uq"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "environment", "issuer_account_id"],
            [
                f"{n('issuer_accounts')}.tenant_id",
                f"{n('issuer_accounts')}.merchant_id",
                f"{n('issuer_accounts')}.environment",
                f"{n('issuer_accounts')}.id",
            ],
            name=n("documents_issuer_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "draft_id"],
            [
                f"{n('invoice_drafts')}.tenant_id",
                f"{n('invoice_drafts')}.merchant_id",
                f"{n('invoice_drafts')}.id",
            ],
            name=n("documents_draft_fk"),
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
                f"{n('documents')}.tenant_id",
                f"{n('documents')}.merchant_id",
                f"{n('documents')}.environment",
                f"{n('documents')}.issuer_account_id",
                f"{n('documents')}.id",
                f"{n('documents')}.correction_mode",
            ],
            name=n("documents_parent_fk"),
        ),
        sa.CheckConstraint(_in("kind", DocumentKind), name=n("documents_kind_ck")),
        sa.CheckConstraint(
            "(kind = 'original' AND original_document_id IS NULL AND parent_correction_mode IS NULL AND adjustment_type IS NULL AND settled_amount_vnd IS NOT NULL AND settled_at IS NOT NULL) OR (kind = 'adjustment' AND tenant_id IS NOT NULL AND merchant_id IS NOT NULL AND environment IS NOT NULL AND issuer_account_id IS NOT NULL AND original_document_id IS NOT NULL AND parent_correction_mode IS NOT NULL AND parent_correction_mode = 'adjust' AND adjustment_type IS NOT NULL) OR (kind = 'replacement' AND tenant_id IS NOT NULL AND merchant_id IS NOT NULL AND environment IS NOT NULL AND issuer_account_id IS NOT NULL AND original_document_id IS NOT NULL AND parent_correction_mode IS NOT NULL AND parent_correction_mode = 'replace' AND adjustment_type IS NULL)",  # noqa: E501
            name=n("documents_kind_shape_ck"),
        ),
        sa.CheckConstraint(
            "correction_mode IS NULL OR " + _in("correction_mode", CorrectionMode),
            name=n("documents_correction_mode_ck"),
        ),
        sa.CheckConstraint(
            "adjustment_type IS NULL OR " + _in("adjustment_type", AdjustmentType),
            name=n("documents_adjustment_type_ck"),
        ),
        sa.CheckConstraint(
            "kind <> 'original' OR total_vnd IS NULL OR total_vnd = settled_amount_vnd",
            name=n("documents_original_total_ck"),
        ),
        sa.CheckConstraint(_in("status", DocumentStatus), name=n("documents_status_ck")),
        sa.CheckConstraint(
            "status IN ('needs_draft', 'blocked') OR (document IS NOT NULL AND total_vnd IS NOT NULL AND buyer_source IS NOT NULL)",  # noqa: E501
            name=n("documents_frozen_ck"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'awaiting_approval', 'pending_provider') OR (request IS NOT NULL AND request_frozen_at IS NOT NULL AND issued_date IS NOT NULL)",  # noqa: E501
            name=n("documents_request_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'queued' OR queued_at IS NOT NULL", name=n("documents_queued_at_ck")
        ),
        sa.CheckConstraint(
            "buyer_source IS NULL OR " + _in("buyer_source", BuyerSource),
            name=n("documents_buyer_source_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'issued' OR (invoice_number IS NOT NULL AND (environment <> 'live' OR tax_authority_code IS NOT NULL))",  # noqa: E501
            name=n("documents_issued_ids_ck"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('replaced', 'cancelled_externally') OR (invoice_number IS NOT NULL AND (environment <> 'live' OR tax_authority_code IS NOT NULL))",  # noqa: E501
            name=n("documents_terminal_ids_ck"),
        ),
        sa.CheckConstraint(
            "settled_amount_vnd IS NULL OR settled_amount_vnd > 0",
            name=n("documents_settled_amount_positive_ck"),
        ),
        sa.CheckConstraint(
            "delivery_mode IS NULL OR " + _in("delivery_mode", DeliveryMode),
            name=n("documents_delivery_mode_ck"),
        ),
        sa.CheckConstraint(
            "kind <> 'original' OR document IS NULL OR delivery_mode IS NOT NULL",
            name=n("documents_delivery_mode_frozen_ck"),
        ),
        sa.CheckConstraint(
            "(approved_at IS NULL) = (approved_by IS NULL) AND (approved_at IS NULL) = (approved_snapshot_sha256 IS NULL) AND (approved_at IS NULL) = (approved_reference_code IS NULL) AND (approved_by IS NULL OR length(btrim(approved_by)) > 0) AND (approved_at IS NULL OR (delivery_mode IS NOT DISTINCT FROM 'manual' AND kind = 'original' AND document IS NOT NULL))",  # noqa: E501
            name=n("documents_approval_ck"),
        ),
        sa.CheckConstraint(
            "approved_reference_code IS NULL OR approved_reference_code = reference_code",
            name=n("documents_approved_reference_ck"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('awaiting_approval', 'approved') OR delivery_mode IS NOT DISTINCT FROM 'manual'",  # noqa: E501
            name=n("documents_manual_status_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'awaiting_approval' OR approved_at IS NULL",
            name=n("documents_awaiting_unapproved_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR approved_at IS NOT NULL",
            name=n("documents_approved_status_ck"),
        ),
        sa.CheckConstraint(
            "delivery_mode IS DISTINCT FROM 'manual' OR status NOT IN ('issued', 'replaced', 'cancelled_externally') OR approved_at IS NOT NULL",  # noqa: E501
            name=n("documents_manual_issue_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'pending_provider' OR pending_since IS NOT NULL",
            name=n("documents_pending_since_ck"),
        ),
        sa.CheckConstraint(
            "issuer_account_id IS NOT NULL OR status = 'blocked'",
            name=n("documents_issuer_required_ck"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND poll_count >= 0 AND lease_generation >= 0 AND request_revision >= 0",
            name=n("documents_counters_ck"),
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR " + _in("last_error_code", IssueErrorCode),
            name=n("documents_error_code_ck"),
        ),
        sa.CheckConstraint(
            "status <> 'cancelled_externally' OR (cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL AND cancel_reason IS NOT NULL)",  # noqa: E501
            name=n("documents_cancelled_ck"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("documents_environment_ck")),
    )
    sa.Index(
        n("documents_original_settlement_uq"),
        documents.c.tenant_id,
        documents.c.settlement_id,
        unique=True,
        postgresql_where=sa.text("kind = 'original'"),
    )
    sa.Index(
        n("documents_reference_uq"),
        documents.c.issuer_account_id,
        documents.c.reference_code,
        unique=True,
    )
    sa.Index(n("documents_due_ix"), documents.c.status, documents.c.next_attempt_at)
    sa.Index(
        n("documents_host_ref_ix"),
        documents.c.tenant_id,
        documents.c.host_ref_type,
        documents.c.host_ref_id,
    )

    receipts = sa.Table(
        n("settlement_receipts"),
        metadata,
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
        _ts("settled_at"),
        _ts("received_at", server_now=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
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
                f"{n('documents')}.tenant_id",
                f"{n('documents')}.merchant_id",
                f"{n('documents')}.environment",
                f"{n('documents')}.settlement_id",
                f"{n('documents')}.settled_amount_vnd",
                f"{n('documents')}.id",
            ],
            name=n("settlement_receipts_document_fk"),
        ),
        sa.CheckConstraint("amount_vnd > 0", name=n("settlement_receipts_amount_ck")),
        sa.CheckConstraint(
            _in("environment", Environment), name=n("settlement_receipts_environment_ck")
        ),
    )
    sa.Index(n("settlement_receipts_settlement_ix"), receipts.c.tenant_id, receipts.c.settlement_id)

    reviews = sa.Table(
        n("review_cases"),
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("details", json_type(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolution", sa.String(32)),
        sa.Column("resolved_by", sa.String(128)),
        sa.Column("resolution_note", sa.Text()),
        _ts("opened_at", server_now=True),
        _ts("resolved_at", nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            [f"{n('documents')}.tenant_id", f"{n('documents')}.id"],
            name=n("review_cases_document_fk"),
        ),
        sa.CheckConstraint(_in("reason", ReviewReason), name=n("review_cases_reason_ck")),
        sa.CheckConstraint(_in("status", ReviewStatus), name=n("review_cases_status_ck")),
        sa.CheckConstraint(
            "resolution IS NULL OR " + _in("resolution", ReviewResolution),
            name=n("review_cases_resolution_ck"),
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolution IS NULL AND resolved_by IS NULL AND resolved_at IS NULL) OR (status = 'resolved' AND resolution IS NOT NULL AND resolved_by IS NOT NULL AND length(btrim(resolved_by)) > 0 AND resolved_at IS NOT NULL)",  # noqa: E501
            name=n("review_cases_resolved_ck"),
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution NOT IN ("
            + ", ".join(
                repr(v.value)
                for v in sorted(NOTE_REQUIRED_RESOLUTIONS, key=lambda value: value.value)
            )
            + ") OR (resolution_note IS NOT NULL AND length(btrim(resolution_note)) > 0)",
            name=n("review_cases_note_ck"),
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution <> 'acknowledge' OR reason IN ("
            + ", ".join(
                repr(v.value)
                for v in sorted(ACKNOWLEDGEABLE_REASONS, key=lambda value: value.value)
            )
            + ")",
            name=n("review_cases_acknowledge_ck"),
        ),
    )
    sa.Index(
        n("review_cases_one_open_uq"),
        reviews.c.document_id,
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    sa.Index(n("review_cases_open_ix"), reviews.c.status, reviews.c.opened_at)
    return InvoiceTables(prefix, issuer, drafts, receipts, documents, reviews)
