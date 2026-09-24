import sqlalchemy as sa

from invoice_module.adapters.sqlalchemy.tables import define_tables
from invoice_module.domain.enums import (
    ACKNOWLEDGEABLE_REASONS,
    NOTE_REQUIRED_RESOLUTIONS,
    DocumentStatus,
    IssueErrorCode,
    ReviewReason,
    ReviewResolution,
)


def test_closed_sets_have_expected_sizes() -> None:
    assert len(DocumentStatus) == 8
    assert len(ReviewReason) == 15
    assert len(ReviewResolution) == 6
    assert len(IssueErrorCode) == 14


def test_config_columns_have_no_default() -> None:
    tables = define_tables(sa.MetaData())
    for table, columns in (
        (
            tables.issuer_accounts,
            (
                "template_code",
                "invoice_series_template",
                "payment_method",
                "buyer_fallback_name",
                "buyer_fallback_address",
                "issue_date_cross_day_action",
            ),
        ),
    ):
        for column in columns:
            assert table.c[column].default is None
            assert table.c[column].server_default is None


def test_no_foreign_key_to_payment_tables() -> None:
    metadata = sa.MetaData()
    define_tables(metadata)
    assert all(
        not fk.target_fullname.startswith("pm_")
        for table in metadata.tables.values()
        for fk in table.foreign_keys
    )


def test_parent_fk_is_six_column_match_simple() -> None:
    tables = define_tables(sa.MetaData())
    fk = next(
        constraint
        for constraint in tables.documents.foreign_key_constraints
        if constraint.name == "inv_documents_parent_fk"
    )
    assert len(fk.column_keys) == 6
    assert fk.match is None


def test_review_checks_match_domain_constants() -> None:
    tables = define_tables(sa.MetaData())
    checks = {
        check.name: str(check.sqltext)
        for check in tables.review_cases.constraints
        if isinstance(check, sa.CheckConstraint)
    }
    for value in ACKNOWLEDGEABLE_REASONS:
        assert value.value in checks["inv_review_cases_acknowledge_ck"]
    for value in NOTE_REQUIRED_RESOLUTIONS:
        assert value.value in checks["inv_review_cases_note_ck"]
