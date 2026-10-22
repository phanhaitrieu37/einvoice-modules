"""Pure request building and response parsing for SePay eInvoice.

Field names follow the create docs (``xuat-hoa-don-dien-tu``: ``CreateInvoiceRequest``,
``Buyer``, ``InvoiceItem``) and the payload the sandbox probe sent successfully. A line of
type 1 needs an ``item_code`` on the sandbox (probe: HTTP 400 ``VALIDATION_ERROR`` without
it); the code comes from the host's draft and is sent only when the host supplied one.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from invoice_module.adapters.sepay import endpoints
from invoice_module.adapters.sepay.endpoints import (
    DRAFT_MARKER,
    DRAFT_MARKER_LOOKUP,
    ENDPOINTS,
    LOOKUP_RETURNS_TOTAL,
    PROVIDER_ISSUED_DATE_PATH,
    check_address_rule,
)
from invoice_module.domain.document import InvoiceDocument
from invoice_module.ports.provider import (
    Accepted,
    AccountFacts,
    IssuerAccountView,
    ProviderInvoice,
    ProviderProgress,
    ProviderRejected,
    ProviderState,
)

VN_TZ = ZoneInfo("Asia/Saigon")
LINE_TYPE_GOODS = 1
# Domain buyer type -> SePay ``Buyer.type`` enum (``personal``, ``company``).
BUYER_TYPES = {"company": "company", "individual": "personal"}
NO_INVOICE_NUMBER = "0"
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
# The one provider message the docs give an instruction for (pages
# ``theo-doi-trang-thai-xuat-hoa-don`` and ``xu-ly-loi``): when ``message`` is this sentence,
# "trạng thái hóa đơn chưa xác định. Không gửi lại yêu cầu - hãy liên hệ SePay". It is
# matched exactly (after strip) at any job status and reported as an error code; no other
# provider text is interpreted.
PROVIDER_UNCONFIRMED_MESSAGE = (
    "Hệ thống chưa xác nhận được kết quả phát hành với nhà cung cấp hóa đơn"
)
PROVIDER_UNCONFIRMED = "provider_unconfirmed"
LOOKUP_REFERENCE_MISMATCH = "lookup_reference_mismatch"


def build_create_payload(
    account: IssuerAccountView,
    doc: InvoiceDocument,
    reference_code: str,
    issued_date: date,
) -> dict[str, object]:
    """The create request for ``doc``; it always issues in one step (``is_draft`` false).

    Deterministic: the same input gives an equal dict with sorted keys. ``issued_date`` is
    sent exactly as the caller froze it.
    """
    if isinstance(issued_date, datetime) or not isinstance(issued_date, date):
        raise TypeError("issued_date must be a date")
    payload: dict[str, object] = {
        "buyer": _buyer(doc.buyer),
        "currency": doc.currency,
        "is_draft": False,
        "issued_date": f"{issued_date.isoformat()} 00:00:00",
        "items": [_item(index, line) for index, line in enumerate(doc.lines, start=1)],
        "invoice_series": account.invoice_series_template.replace(
            "{YY}", f"{issued_date.year % 100:02d}"
        ),
        "payment_method": account.payment_method,
        "provider_account_id": account.provider_account_id,
        "reference_code": reference_code,
        "template_code": account.template_code,
        "total_amount": _int(doc.total_vnd, "total_vnd"),
    }
    if account.seller_store_xid is not None:
        payload["seller_store_xid"] = account.seller_store_xid
    if doc.notes is not None:
        payload["notes"] = doc.notes
    return dict(sorted(payload.items()))


def _buyer(buyer: Mapping[str, object]) -> dict[str, object]:
    result = dict(buyer)
    buyer_type = result.get("type")
    if isinstance(buyer_type, str):
        result["type"] = BUYER_TYPES.get(buyer_type, buyer_type)
    address = result.get("address")
    if isinstance(address, str) and not address.strip():
        if check_address_rule(endpoints.BLANK_ADDRESS_RULE) == "omit":
            del result["address"]
        else:
            result["address"] = ""
    return dict(sorted(result.items()))


def _item(line_number: int, line: Mapping[str, object]) -> dict[str, object]:
    item: dict[str, object] = {
        "before_discount_and_tax_amount": _int(line["before_vnd"], "before_vnd"),
        "discount_amount": _int(line["discount_vnd"], "discount_vnd"),
        "item_name": line["name"],
        "line_number": line_number,
        "line_type": LINE_TYPE_GOODS,
        "quantity": _quantity(line["quantity"]),
        "tax_amount": _int(line["tax_vnd"], "tax_vnd"),
        "tax_rate": _int(line["tax_rate"], "tax_rate"),
        "unit": line["unit"],
        "unit_price": _int(line["unit_price_vnd"], "unit_price_vnd"),
    }
    if line.get("item_code") is not None:
        item["item_code"] = line["item_code"]
    return dict(sorted(item.items()))


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _quantity(value: object) -> int | str:
    """A whole quantity as a JSON integer; a fractional one as its decimal string."""
    if isinstance(value, bool | float):
        raise TypeError("quantity must be a decimal string or an integer")
    quantity = Decimal(str(value))
    if not quantity.is_finite():
        raise ValueError("quantity must be finite")
    if quantity == quantity.to_integral_value():
        return int(quantity)
    return str(quantity)


def _at(body: Any, path: str) -> Any:
    value = body
    for key in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def response_data(body: Any) -> Mapping[str, Any]:
    data = _at(body, "data")
    return data if isinstance(data, Mapping) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _invoice_number(value: Any) -> str | None:
    number = _text(value)
    return None if number is None or number.strip() == NO_INVOICE_NUMBER else number


def _iso_date(value: Any) -> date | None:
    text = _text(value)
    match = _ISO_DATE.match(text) if text else None
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _provider_datetime(value: Any) -> datetime | None:
    """``Y-m-d H:i:s`` in Vietnam time (``retry.next_retry_at``)."""
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=VN_TZ)


def _amount(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("provider amounts must be parsed without float")
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        return None
    if not amount.is_finite() or amount != amount.to_integral_value():
        return None
    return int(amount)


def _is_draft(body: Any, marker: tuple[str, str]) -> bool:
    path, value = marker
    return _at(body, path) == value


def parse_accepted(body: Any) -> Accepted | None:
    """The create acceptance, or ``None`` without ``success: true`` and a tracking code.

    Docs ``xu-ly-loi``: "success: true kèm data.tracking_code nghĩa là yêu cầu đã được tiếp
    nhận".
    """
    if not isinstance(body, Mapping) or body.get("success") is not True:
        return None
    tracking_code = _text(response_data(body).get("tracking_code"))
    return Accepted(tracking_code=tracking_code) if tracking_code is not None else None


def _unconfirmed(message: str | None) -> str | None:
    if message is not None and message.strip() == PROVIDER_UNCONFIRMED_MESSAGE:
        return PROVIDER_UNCONFIRMED
    return None


def parse_progress(body: Any) -> ProviderProgress:
    """A ``check_create`` response (``Pending``, ``Success``, ``Failed``)."""
    data = response_data(body)
    status = data.get("status")
    invoice = data.get("invoice") if isinstance(data.get("invoice"), Mapping) else {}
    message = _text(data.get("message"))
    if status == "Failed":
        return ProviderProgress(
            state="failed", provider_message=message, error_code=_unconfirmed(message)
        )
    if status == "Success":
        state: ProviderState = "draft" if _is_draft(body, DRAFT_MARKER) else "success"
        return ProviderProgress(
            state=state,
            error_code=_unconfirmed(message),
            invoice_number=_invoice_number(invoice.get("invoice_number")),
            tax_authority_code=_text(invoice.get("tax_authority_code")),
            issued_date=_iso_date(_at(body, PROVIDER_ISSUED_DATE_PATH["check"])),
        )
    retry = data.get("retry") if isinstance(data.get("retry"), Mapping) else {}
    return ProviderProgress(
        state="pending",
        next_retry_at=_provider_datetime(retry.get("next_retry_at")),
        error_code=_unconfirmed(message),
    )


def parse_invoice(body: Any, reference_code: str) -> ProviderInvoice:
    """A lookup by reference (``GET /v1/invoices/{reference_code}``).

    ``issued`` is the only success and ``cancelled`` is reported as such. A draft, a failed
    job, or any status this adapter does not know is never reported as success; unknown
    statuses stay ``pending``. An invoice of another reference is refused.
    """
    data = response_data(body)
    status = data.get("status")
    returned = _text(data.get("reference_code"))
    if returned is not None and returned != reference_code:
        raise ProviderRejected(LOOKUP_REFERENCE_MISMATCH)
    if _is_draft(body, DRAFT_MARKER_LOOKUP):
        state: ProviderState = "draft"
    elif status == "issued":
        state = "success"
    elif status == "cancelled":
        state = "cancelled"
    elif status == "failed":
        state = "failed"
    else:
        state = "pending"
    return ProviderInvoice(
        reference_code=reference_code,
        state=state,
        invoice_number=_invoice_number(data.get("invoice_number")),
        tax_authority_code=_text(data.get("tax_authority_code")),
        total_amount_vnd=_amount(data.get("total_amount")) if LOOKUP_RETURNS_TOTAL else None,
        issued_date=_iso_date(_at(body, PROVIDER_ISSUED_DATE_PATH["lookup"])),
        provider_message=_text(data.get("message")) if state == "failed" else None,
    )


def parse_account_facts(
    provider_account_id: str, detail_body: Any, quota_body: Any | None
) -> AccountFacts:
    templates = response_data(detail_body).get("templates")
    pairs = [
        (str(item["template_code"]), str(item["invoice_series"]))
        for item in (templates if isinstance(templates, list) else [])
        if isinstance(item, Mapping)
        and _text(item.get("template_code"))
        and _text(item.get("invoice_series"))
    ]
    quota = None
    if ENDPOINTS["quota"] is not None and quota_body is not None:
        quota = _amount(response_data(quota_body).get("quota_remaining"))
    active = response_data(detail_body).get("active")
    return AccountFacts(
        provider_account_id=provider_account_id,
        templates=tuple(template for template, _ in pairs),
        series=tuple(series for _, series in pairs),
        quota_remaining=quota,
        active=active if isinstance(active, bool) else None,
        tax_authority_approved_date=_iso_date(
            response_data(detail_body).get("tax_authority_approved_date")
        ),
    )
