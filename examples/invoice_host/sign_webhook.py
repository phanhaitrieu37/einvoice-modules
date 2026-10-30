"""A SePay bank-transfer delivery signed the way SePay signs it, for local runs.

The HMAC comes from the installed payment-module wheel, so the example checks the package's
own signature scheme. Every locally signed delivery carries a bank ``referenceCode`` starting
with ``reference_code_prefix`` (``LOCAL-`` by default), so a settlement made from a fake
transfer can always be told apart from a transfer SePay Test really sent.
"""

from __future__ import annotations

import json
import secrets
import time

from payment_module.adapters.sepay.provider import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

LOCAL_REFERENCE_PREFIX = "LOCAL-"


def signed_delivery(
    secret: str,
    account_number: str,
    amount_vnd: int,
    code: str | None,
    content: str,
    *,
    reference_code_prefix: str = LOCAL_REFERENCE_PREFIX,
) -> tuple[bytes, dict[str, str], str]:
    """``(raw_body, headers, bank_reference)`` of an incoming Vietcombank transfer."""
    bank_reference = f"{reference_code_prefix}FT{secrets.randbelow(10**10):010d}"
    raw = json.dumps(
        {
            "id": 10**8 + secrets.randbelow(9 * 10**8),
            "gateway": "VCB",
            "transactionDate": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accountNumber": account_number,
            "subAccount": None,
            "code": code,
            "content": content,
            "transferType": "in",
            "transferAmount": amount_vnd,
            "referenceCode": bank_reference,
        }
    ).encode()
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(raw, secret, timestamp),
    }
    return raw, headers, bank_reference
