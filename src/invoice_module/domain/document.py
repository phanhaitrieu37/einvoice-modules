import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from invoice_module.domain.enums import BuyerSource, Environment
from invoice_module.domain.errors import InvalidTaxCode
from invoice_module.domain.money import LineInput, check_declared_total, compute_line

_TAX_CODE = re.compile(r"^\d{10}(-?\d{3})?$")
ALLOWED_FALLBACK_EXTRA_KEYS = frozenset({"legal_name", "type", "phone", "email", "national_id"})


@dataclass(frozen=True)
class Buyer:
    name: str | None = None
    legal_name: str | None = None
    tax_code: str | None = None
    address: str | None = None
    email: str | None = None
    phone: str | None = None
    national_id: str | None = None
    type: Literal["company", "individual"] | None = None


def validate_buyer(buyer: Buyer) -> Buyer:
    if buyer.tax_code:
        if not _TAX_CODE.fullmatch(buyer.tax_code):
            raise InvalidTaxCode("invalid tax code")
        if not (buyer.legal_name or buyer.name) or not buyer.address:
            raise ValueError("a tax code requires a buyer name and address")
        if buyer.type == "individual":
            raise ValueError("an individual buyer cannot have a tax code")
        if buyer.type is None:
            return Buyer(**(buyer.__dict__ | {"type": "company"}))
    return buyer


@dataclass(frozen=True)
class DraftData:
    buyer: Buyer | None
    lines: tuple[LineInput, ...]
    declared_total_vnd: int
    notes: str | None = None


class _IssuerAccountView(Protocol):
    buyer_fallback_name: str
    buyer_fallback_address: str
    buyer_fallback_extra: Mapping[str, str] | None


@dataclass(frozen=True)
class InvoiceDocument:
    buyer: dict[str, object]
    buyer_source: BuyerSource
    lines: tuple[dict[str, object], ...]
    total_vnd: int
    currency: str = "VND"
    notes: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "buyer": self.buyer,
            "buyer_source": self.buyer_source.value,
            "currency": self.currency,
            "lines": list(self.lines),
            "notes": self.notes,
            "total_vnd": self.total_vnd,
        }

    @classmethod
    def from_json(cls, value: dict[str, object]) -> "InvoiceDocument":
        return cls(
            buyer=dict(value["buyer"]),  # type: ignore[arg-type]
            buyer_source=BuyerSource(str(value["buyer_source"])),
            lines=tuple(dict(line) for line in value["lines"]),  # type: ignore[arg-type]
            total_vnd=int(value["total_vnd"]),
            currency=str(value["currency"]),
            notes=value["notes"] if isinstance(value["notes"], str) else None,
        )

    def canonical_json(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def freeze_document(draft: DraftData, account: _IssuerAccountView) -> InvoiceDocument:
    check_declared_total(draft.declared_total_vnd, draft.lines)
    if draft.buyer is None:
        extra = account.buyer_fallback_extra or {}
        unexpected = extra.keys() - ALLOWED_FALLBACK_EXTRA_KEYS
        if unexpected:
            raise ValueError(f"unsupported buyer fallback fields: {sorted(unexpected)}")
        buyer: dict[str, object] = {
            "name": account.buyer_fallback_name,
            "address": account.buyer_fallback_address,
            **extra,
        }
        source = BuyerSource.FALLBACK
    else:
        valid_buyer = validate_buyer(draft.buyer)
        buyer = {key: value for key, value in valid_buyer.__dict__.items() if value is not None}
        source = BuyerSource.HOST

    frozen_lines: list[dict[str, object]] = []
    for index, line in enumerate(draft.lines):
        amounts = compute_line(line, index)
        frozen_lines.append(
            {
                "name": line.name,
                "unit": line.unit,
                "item_code": line.item_code,
                "quantity": str(line.quantity),
                "unit_price_vnd": line.unit_price_vnd,
                "discount_vnd": line.discount_vnd,
                "tax_rate": line.tax_rate,
                "before_vnd": amounts.before_vnd,
                "taxable_vnd": amounts.taxable_vnd,
                "tax_vnd": amounts.tax_vnd,
                "line_total_vnd": amounts.line_total_vnd,
            }
        )
    return InvoiceDocument(
        buyer=buyer,
        buyer_source=source,
        lines=tuple(frozen_lines),
        total_vnd=draft.declared_total_vnd,
        notes=draft.notes,
    )


@dataclass(frozen=True)
class SettledPayment:
    event_id: object
    tenant_id: str
    merchant_id: object
    environment: Environment
    intent_id: object
    settlement_id: object
    transaction_id: object
    amount_vnd: int
    settled_at: datetime
    origin: str
    host_ref_type: str
    host_ref_id: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.amount_vnd, bool)
            or not isinstance(self.amount_vnd, int)
            or self.amount_vnd <= 0
        ):
            raise ValueError("amount_vnd must be a positive integer")
        if self.settled_at.tzinfo is None or self.settled_at.utcoffset() is None:
            raise ValueError("settled_at must be timezone-aware")
