"""Documented SePay eInvoice API paths; evidence status remains pending sandbox probes."""

from __future__ import annotations

CANDIDATES: dict[str, dict[str, str | bool | None]] = {
    "token": {"method": "POST", "path": "/v1/token", "source": "quickstart", "verified": False},
    "provider_accounts": {
        "method": "GET",
        "path": "/v1/provider-accounts",
        "source": "quickstart",
        "verified": False,
    },
    "provider_account_detail": {
        "method": "GET",
        "path": "/v1/provider-accounts/{id}",
        "source": "quickstart linked account-detail docs",
        "verified": False,
    },
    "create": {
        "method": "POST",
        "path": "/v1/invoices/create",
        "source": "quickstart",
        "verified": False,
    },
    "check_create": {
        "method": "GET",
        "path": "/v1/invoices/create/check/{tracking_code}",
        "source": "quickstart",
        "verified": False,
    },
    "get_by_reference": {
        "method": "GET",
        "path": "/v1/invoices/{reference_code}",
        "source": "detail endpoint; reference lookup guess",
        "verified": False,
    },
    "get_by_reference_list": {
        "method": "GET",
        "path": "/v1/invoices?reference_code={reference_code}",
        "source": "invoice list filter guess",
        "verified": False,
    },
    "download": {
        "method": "GET",
        "path": "/v1/invoices/{tracking_code}/download",
        "source": "invoice docs",
        "parameter_guess": "guess: format=pdf|xml when the response has no file URL",
        "verified": False,
    },
    "quota": {"method": "GET", "path": "/v1/quota", "source": "invoice docs", "verified": False},
    "publish": {
        "method": "POST",
        "path": "/v1/invoices/publish",
        "source": "invoice docs",
        "verified": False,
    },
}
