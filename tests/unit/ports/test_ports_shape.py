import ast
from pathlib import Path

from invoice_module.ports.provider import (
    ClientCredentials,
    EInvoiceProvider,
    IssuerAccountView,
    ProviderInvoice,
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


def test_credentials_repr_hides_secret() -> None:
    assert "sensitive" not in repr(ClientCredentials("id", "sensitive"))


def test_core_has_no_optional_imports() -> None:
    root = Path("src/invoice_module")
    for area in ("domain", "ports"):
        for path in (root / area).glob("*.py"):
            tree = ast.parse(path.read_text())
            imports = [
                node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
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
