import ast
import inspect
from pathlib import Path
from typing import get_type_hints

import invoice_module
from invoice_module.builder import build_invoice_module
from invoice_module.domain.document import _IssuerAccountView
from invoice_module.ports.provider import (
    ClientCredentials,
    EInvoiceProvider,
    IssuerAccountView,
    ProviderInvoice,
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
