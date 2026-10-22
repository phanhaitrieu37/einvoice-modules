import ast
import inspect
from datetime import date
from pathlib import Path
from typing import get_args, get_type_hints

import invoice_module
from invoice_module.builder import build_invoice_module
from invoice_module.domain.document import _IssuerAccountView
from invoice_module.ports import provider as provider_port
from invoice_module.ports.provider import (
    AccountFacts,
    ClientCredentials,
    EInvoiceProvider,
    IssuerAccountView,
    ProviderInvoice,
    ProviderProgress,
    ProviderRejected,
    ProviderTransient,
)


def test_provider_protocol_methods() -> None:
    expected = {
        "create",
        "build_create_payload",
        "check_create",
        "find_by_reference",
        "adjust",
        "replace",
        "build_adjust_payload",
        "build_replace_payload",
        "fetch_artifacts",
        "describe_account",
    }
    assert expected <= set(EInvoiceProvider.__dict__)


def test_account_view_has_operational_fields() -> None:
    names = IssuerAccountView.__dataclass_fields__
    assert {
        "buyer_fallback_name",
        "buyer_fallback_address",
        "issue_date_cross_day_action",
    } <= names.keys()
    for field in ("buyer_fallback_name", "buyer_fallback_address", "issue_date_cross_day_action"):
        assert names[field].default is names[field].default_factory


def test_provider_invoice_has_total() -> None:
    assert "total_amount_vnd" in ProviderInvoice.__dataclass_fields__


def _states(result: type) -> set[str]:
    return set(get_args(get_type_hints(result)["state"]))


def test_build_create_payload_has_no_draft_option() -> None:
    parameters = inspect.signature(EInvoiceProvider.build_create_payload).parameters
    assert list(parameters) == ["self", "account", "doc", "reference_code", "issued_date"]
    assert get_type_hints(EInvoiceProvider.build_create_payload)["issued_date"] is date


def test_port_has_no_issue_draft_or_check_issue() -> None:
    assert not {"issue_draft", "check_issue"} & set(dir(EInvoiceProvider))
    assert not hasattr(provider_port, "IssueTargetNotDraft")
    assert not hasattr(provider_port, "IssueRejectedRetryable")
    assert "unconfirmed" not in _states(ProviderProgress)
    assert "unconfirmed" not in _states(ProviderInvoice)


def test_provider_states_include_draft() -> None:
    expected = {"pending", "success", "failed", "draft", "cancelled"}
    assert _states(ProviderProgress) == expected
    assert _states(ProviderInvoice) == expected


def test_account_facts_carry_activation_and_approval_date() -> None:
    hints = get_type_hints(AccountFacts)
    assert hints["active"] == bool | None
    assert hints["tax_authority_approved_date"] == date | None
    facts = AccountFacts(provider_account_id="acc", templates=("1",), series=("C26TSE",))
    assert facts.active is None
    assert facts.tax_authority_approved_date is None
    approved = AccountFacts(
        provider_account_id="acc",
        templates=("1",),
        series=("C26TSE",),
        active=True,
        tax_authority_approved_date=date(2026, 4, 20),
    )
    assert approved.active is True
    assert approved.tax_authority_approved_date == date(2026, 4, 20)


def test_provider_invoice_has_issued_date() -> None:
    assert get_type_hints(ProviderInvoice)["issued_date"] == date | None
    assert ProviderInvoice(reference_code="ref-1", state="success").issued_date is None
    issued = ProviderInvoice(reference_code="ref-1", state="success", issued_date=date(2026, 10, 7))
    assert issued.issued_date == date(2026, 10, 7)


def test_provider_progress_has_issued_date() -> None:
    assert get_type_hints(ProviderProgress)["issued_date"] == date | None
    assert ProviderProgress(state="success").issued_date is None
    issued = ProviderProgress(state="success", issued_date=date(2026, 10, 7))
    assert issued.issued_date == date(2026, 10, 7)


def test_provider_results_carry_provider_message() -> None:
    message = "Ngày lập hóa đơn không hợp lệ"
    for result in (ProviderProgress, ProviderInvoice):
        assert get_type_hints(result)["provider_message"] == str | None
    assert ProviderProgress(state="failed").provider_message is None
    assert ProviderInvoice(reference_code="ref-1", state="failed").provider_message is None
    assert ProviderProgress(state="failed", provider_message=message).provider_message == message
    failed = ProviderInvoice(reference_code="ref-1", state="failed", provider_message=message)
    assert failed.provider_message == message


def test_credentials_repr_hides_secret() -> None:
    assert "sensitive" not in repr(ClientCredentials("id", "sensitive"))


def test_provider_errors_preserve_messages() -> None:
    assert str(ProviderRejected("bad_request")) == "bad_request"
    assert str(ProviderTransient()) == "transient provider error"


def test_builder_requires_clock() -> None:
    assert (
        inspect.signature(build_invoice_module).parameters["clock"].default
        is inspect.Parameter.empty
    )


def test_document_account_view_accepts_mapping() -> None:
    assert "Mapping" in str(get_type_hints(_IssuerAccountView)["buyer_fallback_extra"])


def test_core_has_no_optional_imports() -> None:
    root = Path(invoice_module.__file__).resolve().parent
    scanned = 0
    for area in ("domain", "ports"):
        for path in (root / area).rglob("*.py"):
            scanned += 1
            tree = ast.parse(path.read_text())
            imports = [
                node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            ]
            relative_imports = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.level > 0
            ]
            imports.extend(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            assert not any(
                name.startswith(("payment_module", "sqlalchemy", "httpx")) for name in imports
            )
            assert not relative_imports
    assert scanned > 0
