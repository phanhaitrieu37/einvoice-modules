"""Candidate SePay eInvoice API paths from public docs; unverified until probed."""

from __future__ import annotations

CANDIDATES: dict[str, dict[str, str | bool | None]] = {
    "token": {"method": "POST", "path": "/oauth/token", "verified": False},
    "provider_accounts": {"method": "GET", "path": "/v1/provider-accounts", "verified": False},
    "create": {"method": "POST", "path": "/v1/invoices", "verified": False},
    "check_create": {
        "method": "GET",
        "path": "/v1/invoices/issue/check/{tracking_code}",
        "verified": False,
    },
    "get_by_reference": {
        "method": "GET",
        "path": "/v1/invoices/{reference_code}",
        "verified": False,
    },
    "download": {"method": "GET", "path": "/v1/invoices/{id}/download", "verified": False},
    "quota": {"method": "GET", "path": "/v1/quotas/check", "verified": False},
    "publish": {"method": "POST", "path": "/v1/invoices/publish", "verified": False},
}
