import json

import pytest

from tools.einvoice_probe.redact import redact


def test_redact_tokens() -> None:
    result = redact(
        {
            "access_token": "secret",
            "client_id": "secret",
            "client_secret": "secret",
            "Authorization": "Bearer secret",
            "token": "secret",
        }
    )
    assert result == {
        "access_token": "REDACTED",
        "client_id": "REDACTED",
        "client_secret": "REDACTED",
        "Authorization": "REDACTED",
        "token": "REDACTED",
    }


def test_redact_preserves_endpoint_descriptor_named_token() -> None:
    descriptor = {"method": "POST", "path": "/v1/token", "verified": True}
    assert redact({"token": descriptor}) == {"token": descriptor}


def test_redact_secret_descriptor_with_extra_field() -> None:
    assert redact({"token": {"method": "x", "path": "/", "verified": True, "extra": "s"}}) == {
        "token": "REDACTED"
    }


def test_redact_buyer_and_seller_pii() -> None:
    result = redact(
        {
            "buyer": {
                "name": "Jane",
                "email": "jane@real.test",
                "tax_code": "1234567890",
                "national_id": "001099012345",
            },
            "seller": {"legal_name": "Seller", "address": "Street"},
        }
    )
    assert result == {
        "buyer": {
            "name": "REDACTED",
            "email": "REDACTED",
            "tax_code": "0000000000",
            "national_id": "REDACTED",
        },
        "seller": {"legal_name": "REDACTED", "address": "REDACTED"},
    }


def test_redact_seller_store_name() -> None:
    assert redact({"seller_store": {"name": "Private branch", "code": "branch-a"}}) == {
        "seller_store": {"name": "REDACTED", "code": "branch-a"}
    }


def test_seller_store_name_is_redacted_in_saved_probe_json(tmp_path) -> None:
    from tools.einvoice_probe.scenarios import write_json

    path = write_json(
        tmp_path, "store", {"seller_store": {"name": "Private counter", "code": "ST-1"}}
    )
    saved = path.read_text(encoding="utf-8")
    assert "Private counter" not in saved
    assert "REDACTED" in saved


def test_redact_nested_lists() -> None:
    result = redact({"data": [{"buyer": {"phone": "0900000000"}}, {"provider_account_id": "abc"}]})
    assert result == {
        "data": [
            {"buyer": {"phone": "REDACTED"}},
            {"provider_account_id": "00000000-0000-0000-0000-000000000000"},
        ]
    }


def test_redact_provider_account_ids() -> None:
    zero_uuid = "00000000-0000-0000-0000-000000000000"
    result = redact(
        {
            "items": [{"id": "provider-list-id", "provider": "matbao"}],
            "data": {"id": "provider-detail-id", "provider": "matbao"},
        }
    )
    assert result == {
        "items": [{"id": zero_uuid, "provider": "matbao"}],
        "data": {"id": zero_uuid, "provider": "matbao"},
    }


def test_redact_jwt_anywhere() -> None:
    assert redact({"message": "bad token eyJabcdefghijk.payload.signature at provider"}) == {
        "message": "bad token REDACTED_JWT at provider"
    }


SYNTHETIC_ADDRESS = "SYNTHETIC NONEMPTY ADDRESS"


def test_redact_keeps_blank_buyer_address_states(tmp_path) -> None:
    from tools.einvoice_probe.scenarios import write_json

    calls = {
        "missing": {"buyer": {"name": "Fallback"}},
        "empty": {"buyer": {"name": "Fallback", "address": ""}},
        "null": {"buyer": {"name": "Fallback", "address": None}},
    }
    for name, buyer in calls.items():
        written = write_json(tmp_path, name, {"body": {"data": {"invoice": buyer}}})
        saved = json.loads(written.read_text(encoding="utf-8"))["body"]["data"]["invoice"]
        assert saved["buyer"]["name"] == "REDACTED"
        assert {key: value for key, value in saved["buyer"].items() if key != "name"} == {
            key: value for key, value in buyer["buyer"].items() if key != "name"
        }
    # The exception is narrow: a blank seller address is still masked.
    assert redact({"seller": {"address": ""}}) == {"seller": {"address": "REDACTED"}}


def test_redact_masks_nonempty_buyer_address(tmp_path) -> None:
    from tools.einvoice_probe.scenarios import write_json

    written = write_json(
        tmp_path,
        "lookup",
        {"body": {"data": {"buyer": {"name": "Fallback", "address": SYNTHETIC_ADDRESS}}}},
    )
    saved = written.read_text(encoding="utf-8")
    assert SYNTHETIC_ADDRESS not in saved
    assert json.loads(saved)["body"]["data"]["buyer"]["address"] == "REDACTED"
    assert redact({"buyer": {"address": " "}}) == {"buyer": {"address": "REDACTED"}}


def test_address_state_never_carries_text_through_write_json(tmp_path) -> None:
    from tools.einvoice_probe.scenarios import write_json

    findings = {
        "variant_omitted": {
            "check_address": {"path": "body.data.invoice.buyer.address", "state": "nonempty"},
            "lookup_address": {"path": "body.data.buyer.address", "value": SYNTHETIC_ADDRESS},
        },
        "blank": {"path": "body.data.buyer.address", "value": ""},
        "classifier_paths": {
            "lookup_address": "buyer_address_*_lookup.json:body.data.buyer.address"
        },
    }
    written = write_json(tmp_path, "findings_buyer", findings)
    saved = written.read_text(encoding="utf-8")
    assert SYNTHETIC_ADDRESS not in saved
    loaded = json.loads(saved)
    assert (
        loaded["variant_omitted"]["check_address"] == findings["variant_omitted"]["check_address"]
    )
    assert loaded["variant_omitted"]["lookup_address"]["value"] == "REDACTED"
    assert loaded["blank"] == findings["blank"]
    assert loaded["classifier_paths"] == findings["classifier_paths"]


SIBLINGS = {
    "message": "bad 0312345678 at https://portal.test/a?sig=abc",
    "token": "sekret",
    "tax_code": "0312345678",
}
REDACTED_SIBLINGS = {
    "message": "bad 0000000000 at https://portal.test/a?REDACTED",
    "token": "REDACTED",
    "tax_code": "0000000000",
}


@pytest.mark.parametrize(
    "path",
    ["body.data.buyer.address", "body.data.invoice.buyer_address", "buyer.name"],
    ids=["address", "suffixed-address", "name"],
)
def test_field_descriptor_masks_value_and_still_redacts_siblings(tmp_path, path) -> None:
    from tools.einvoice_probe.scenarios import write_json

    written = write_json(
        tmp_path,
        "errors",
        {"errors": [{"path": path, "value": SYNTHETIC_ADDRESS, **SIBLINGS}]},
    )
    saved = written.read_text(encoding="utf-8")
    for text in (SYNTHETIC_ADDRESS, "sekret", "0312345678", "sig=abc"):
        assert text not in saved
    assert json.loads(saved)["errors"] == [{"path": path, "value": "REDACTED", **REDACTED_SIBLINGS}]


def test_blank_buyer_address_keeps_sibling_redaction(tmp_path) -> None:
    from tools.einvoice_probe.scenarios import write_json

    findings = {
        "blank": {"path": "body.data.buyer.address", "value": "", **SIBLINGS},
        "body": {"data": {"buyer": {"name": "Fallback", "address": "", **SIBLINGS}}},
        "filled": {"data": {"buyer": {"address": SYNTHETIC_ADDRESS, **SIBLINGS}}},
    }
    written = write_json(tmp_path, "blank", findings)
    saved = json.loads(written.read_text(encoding="utf-8"))
    assert saved["blank"] == {"path": "body.data.buyer.address", "value": "", **REDACTED_SIBLINGS}
    assert saved["body"]["data"]["buyer"] == {
        "name": "REDACTED",
        "address": "",
        "message": "bad 0000000000 at https://portal.test/a?REDACTED",
        "token": "REDACTED",
        "tax_code": "0000000000",
    }
    assert saved["filled"]["data"]["buyer"] == {"address": "REDACTED", **REDACTED_SIBLINGS}
