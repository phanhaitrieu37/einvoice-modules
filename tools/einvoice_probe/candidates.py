"""Documented SePay eInvoice API paths; evidence status remains pending sandbox probes."""

from __future__ import annotations

CANDIDATES: dict[str, dict[str, str | bool | None]] = {
    "token": {
        "method": "POST",
        "path": "/v1/token",
        "source": "tao-token; bat-dau-nhanh",
        "verified": False,
    },
    "provider_accounts": {
        "method": "GET",
        "path": "/v1/provider-accounts",
        "source": "bat-dau-nhanh",
        "verified": False,
    },
    "provider_account_detail": {
        "method": "GET",
        "path": "/v1/provider-accounts/{id}",
        "source": "chi-tiet-tai-khoan",
        "verified": False,
    },
    "create": {
        "method": "POST",
        "path": "/v1/invoices/create",
        "source": "xuat-hoa-don-dien-tu",
        "verified": False,
    },
    "check_create": {
        "method": "GET",
        "path": "/v1/invoices/create/check/{tracking_code}",
        "source": "bat-dau-nhanh",
        "verified": False,
    },
    "get_by_reference": {
        "method": "GET",
        "path": "/v1/invoices/{reference_code}",
        "source": "chi-tiet-hoa-don",
        "verified": False,
    },
    "download": {
        "method": "GET",
        "path": "/v1/invoices/{tracking_code}/download",
        "source": "tai-hoa-don",
        "parameter": "type=pdf|xml",
        "verified": False,
    },
    "list_invoices": {
        "method": "GET",
        "path": "/v1/invoices",
        "source": "danh-sach-hoa-don",
        "verified": False,
    },
    "quota": {
        "method": "GET",
        "path": "/v1/usage",
        "source": "kiem-tra-han-ngach",
        "verified": False,
    },
    "publish": {
        "method": "POST",
        "path": "/v1/invoices/issue",
        "source": "phat-hanh-hoa-don-dien-tu",
        "verified": False,
    },
}
