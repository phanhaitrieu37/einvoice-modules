import json
import re
from pathlib import Path

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "sepay_einvoice"
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PII_DIGITS = re.compile(r"(?<!\d)(?:\d{10}|\d{13})(?!\d)")
SECRET_KEYS = {"access_token", "token", "client_id", "client_secret", "authorization"}


def test_fixtures_have_no_secret_or_pii() -> None:
    for path in FIXTURE_DIR.glob("*.json"):
        raw = path.read_text(encoding="utf-8")
        assert JWT.search(raw) is None, path
        assert all(email.endswith("@example.com") for email in EMAIL.findall(raw)), path
        assert all(value == "0000000000" for value in PII_DIGITS.findall(raw)), path
        payload = json.loads(raw)
        for key, value in _walk(payload):
            if key.lower() in SECRET_KEYS:
                assert value == "REDACTED", path


def _walk(value: object):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key), nested
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)
