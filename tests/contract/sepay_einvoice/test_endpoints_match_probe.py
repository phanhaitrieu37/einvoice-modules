import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from invoice_module.adapters.sepay import endpoints
from invoice_module.adapters.sepay.endpoints import (
    ENDPOINTS,
    PROVIDER_ACCOUNT_DETAIL,
    check_address_rule,
    check_download_returns,
)
from invoice_module.adapters.sepay.provider import SePayEInvoiceProvider
from invoice_module.ports.provider import ProviderConfigError

pytestmark = pytest.mark.contract

RECORDED = Path(__file__).parents[2] / "fixtures" / "sepay_einvoice"


def _load(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def _body_path(path: str) -> str:
    assert path.startswith("body.")
    return path.removeprefix("body.")


def test_endpoints_match_probe_file() -> None:
    probe = _load("endpoints.json")
    used = {"token", "provider_accounts", "create", "check_create", "get_by_reference"}
    used |= {"download", "quota"}
    assert set(ENDPOINTS) == used
    for operation, route in ENDPOINTS.items():
        recorded = probe[operation]
        if recorded["verified"] is not True or recorded["path"] is None:
            assert route is None, operation
        else:
            assert route == (recorded["method"], recorded["path"]), operation


def test_provider_account_detail_route_was_read_by_the_probe() -> None:
    listing = _load("provider_accounts.json")
    details = listing["provider_account_details"]
    assert details and all(detail["status"] == 200 for detail in details)
    assert all(detail["body"]["data"]["templates"] for detail in details)
    assert ("GET", ENDPOINTS["provider_accounts"][1] + "/{id}") == PROVIDER_ACCOUNT_DETAIL


def test_findings_copied_into_adapter_constants() -> None:
    findings = _load("findings.json")
    draft = _load("findings_draft.json")
    buyer = _load("findings_buyer.json")
    assert findings["token_ttl_seconds"] == endpoints.TOKEN_TTL_SECONDS
    assert findings["lookup_returns_total"] is endpoints.LOOKUP_RETURNS_TOTAL
    assert findings["download_returns"] == endpoints.DOWNLOAD_RETURNS
    assert findings["quota_endpoint"] is (ENDPOINTS["quota"] is not None)
    assert (
        _body_path(draft["draft_marker"]["path"]),
        draft["draft_marker"]["value"],
    ) == endpoints.DRAFT_MARKER
    assert (
        _body_path(draft["draft_marker_lookup"]["path"]),
        draft["draft_marker_lookup"]["value"],
    ) == endpoints.DRAFT_MARKER_LOOKUP
    assert {
        key: _body_path(value) for key, value in draft["provider_issued_date_path"].items()
    } == endpoints.PROVIDER_ISSUED_DATE_PATH
    assert buyer["address_rule"] == endpoints.BLANK_ADDRESS_RULE


@pytest.mark.parametrize("rule", ["neither", "inconclusive", "", "OMIT"])
def test_unknown_blank_address_rule_is_a_config_error(rule: str) -> None:
    with pytest.raises(ProviderConfigError):
        check_address_rule(rule)


def test_download_kind_other_than_bytes_is_a_config_error() -> None:
    with pytest.raises(ProviderConfigError):
        check_download_returns("url")


def test_import_rejects_an_unknown_blank_address_rule() -> None:
    source = inspect.getsource(endpoints).replace(
        'BLANK_ADDRESS_RULE = "omit"', 'BLANK_ADDRESS_RULE = "neither"'
    )
    assert 'BLANK_ADDRESS_RULE = "neither"' in source
    spec = importlib.util.spec_from_loader("sepay_endpoints_copy", loader=None)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(ProviderConfigError):
        exec(compile(source, "sepay_endpoints_copy", "exec"), module.__dict__)


def test_v01_adapter_has_no_publish_or_check_issue() -> None:
    assert "publish" not in ENDPOINTS
    assert "check_issue" not in ENDPOINTS
    assert not hasattr(SePayEInvoiceProvider, "issue_draft")
    assert not hasattr(SePayEInvoiceProvider, "check_issue")
    assert not any("/issue" in route[1] for route in ENDPOINTS.values() if route is not None)
