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


def test_redact_jwt_anywhere() -> None:
    assert redact({"message": "bad token eyJabcdefghijk.payload.signature at provider"}) == {
        "message": "bad token REDACTED_JWT at provider"
    }
