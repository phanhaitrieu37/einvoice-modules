import json
import re
import sys
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "sepay_einvoice"
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TAX_ID = re.compile(r"(?<!\d)(?:\d{10}|\d{13})(?!\d)")
UUID = re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
URL_QUERY = re.compile(r"https?://[^\s\"'<>?#]+\?([^\s\"'<>#]+)", re.IGNORECASE)
SECRET_KEYS = {"access_token", "token", "client_id", "client_secret", "authorization"}
TIME_KEYS = {"time", "timestamp", "created_at", "updated_at", "issued_date", "date"}


def test_fixtures_have_no_secret_or_pii() -> None:
    for path in FIXTURE_DIR.glob("*.json"):
        raw = path.read_text(encoding="utf-8")
        assert JWT.search(raw) is None, path
        assert all(match.group(1) == "REDACTED" for match in URL_QUERY.finditer(raw)), path
        assert all(email.endswith("@example.com") for email in EMAIL.findall(raw)), path
        payload = json.loads(raw)
        for key, value in _walk(payload):
            normalized = key.lower()
            if normalized in SECRET_KEYS:
                if isinstance(value, str):
                    assert value == "REDACTED", path
                else:
                    assert (
                        path.name == "endpoints.json"
                        and normalized == "token"
                        and isinstance(value, dict)
                    ), path
            if (
                isinstance(value, str)
                and normalized != "sha256"
                and not any(part in normalized for part in TIME_KEYS)
            ):
                assert all(match.group() == "0000000000" for match in TAX_ID.finditer(value)), path
            if normalized == "tax_code":
                assert value == "0000000000", path
        for account in _provider_accounts(payload):
            value = account.get("id")
            if isinstance(value, str) and UUID.fullmatch(value):
                assert value == "00000000-0000-0000-0000-000000000000", path
        for key, value, in_party in _walk_context(payload):
            if key.lower() == "provider_account_id":
                assert value == "00000000-0000-0000-0000-000000000000", path
            if in_party and key.lower() in {
                "name",
                "legal_name",
                "address",
                "national_id",
                "phone",
                "email",
            }:
                assert value == "REDACTED", path


def test_ten_digit_timestamps_are_allowed_in_fixtures(tmp_path, monkeypatch) -> None:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "timestamp.json").write_text('{"created_at": "1690000000"}\n', encoding="utf-8")
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "FIXTURE_DIR", fixture_dir)
    test_fixtures_have_no_secret_or_pii()


def test_fixture_scan_rejects_unredacted_access_token(tmp_path, monkeypatch) -> None:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "token.json").write_text(
        '{"access_token":"sensitive-value"}\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "FIXTURE_DIR", fixture_dir)
    with pytest.raises(AssertionError):
        test_fixtures_have_no_secret_or_pii()


def test_fixture_scan_rejects_unredacted_provider_account_id(tmp_path, monkeypatch) -> None:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "accounts.json").write_text(
        '{"provider":"sepay","id":"12345678-1234-1234-1234-123456789abc"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "FIXTURE_DIR", fixture_dir)
    with pytest.raises(AssertionError):
        test_fixtures_have_no_secret_or_pii()


def test_fixture_scan_rejects_non_string_secret_outside_endpoints_shape(
    tmp_path, monkeypatch
) -> None:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "unexpected.json").write_text(
        '{"token":{"unexpected":true}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "FIXTURE_DIR", fixture_dir)
    with pytest.raises(AssertionError):
        test_fixtures_have_no_secret_or_pii()


@pytest.mark.parametrize(
    "payload",
    [
        '{"buyer":{"name":"Jane"}}',
        '{"seller":{"legal_name":"Seller Inc"}}',
        '{"buyer":{"address":"Private Street"}}',
        '{"seller_store":{"name":"Private Counter"}}',
        '{"buyer":{"phone":"+84901234567"}}',
        '{"buyer":{"email":"buyer@example.com"}}',
        '{"url":"https://example.test/invoice?token=private"}',
        '{"provider":"sepay","provider_account_id":"12345678-1234-1234-1234-123456789abc"}',
        '{"metadata":{"provider_account_id":"private-account-id"}}',
        '{"buyer":{"national_id":"001099012345"}}',
    ],
    ids=(
        "buyer-name",
        "legal-name",
        "address",
        "seller-store-name",
        "phone",
        "email",
        "url-query",
        "provider-account-id-in-provider",
        "provider-account-id-anywhere",
        "national-id",
    ),
)
def test_fixture_scan_rejects_each_single_pii_violation(payload, tmp_path, monkeypatch) -> None:
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "violation.json").write_text(payload + "\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "FIXTURE_DIR", fixture_dir)
    with pytest.raises(AssertionError):
        test_fixtures_have_no_secret_or_pii()


def _walk(value: object):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key), nested
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _provider_accounts(value: object):
    if isinstance(value, dict):
        if "provider" in value:
            yield value
        for nested in value.values():
            yield from _provider_accounts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _provider_accounts(nested)


def _walk_context(value: object, *, in_party: bool = False):
    if isinstance(value, dict):
        for key, nested in value.items():
            party = in_party or key.lower() in {"buyer", "seller", "seller_store"}
            yield key, nested, party
            yield from _walk_context(nested, in_party=party)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_context(nested, in_party=in_party)
