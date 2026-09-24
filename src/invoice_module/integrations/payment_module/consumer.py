from datetime import datetime
from typing import Any
from uuid import UUID

from payment_module.ports.publisher import OutboxEventView

from invoice_module.application.record_settled_payment import RecordSettledPayment
from invoice_module.domain.document import SettledPayment
from invoice_module.domain.enums import Environment
from invoice_module.domain.errors import ScopeMismatch, UnsupportedEventVersion


class PaymentSettledConsumer:
    def __init__(self, record: RecordSettledPayment) -> None:
        self._record = record

    async def publish(self, event: OutboxEventView) -> None:
        if event.event_type != "PaymentSettled":
            return
        if isinstance(event.schema_version, bool) or event.schema_version != 1:
            raise UnsupportedEventVersion(
                f"unsupported PaymentSettled schema version: {event.schema_version}"
            )
        payload = event.payload
        if str(event.event_id) != payload.get("event_id"):
            raise ScopeMismatch("event id does not match payload")
        scope = event.trusted_scope
        payload_scope = payload.get("trusted_scope")
        expected = {
            "tenant_id": payload.get("tenant_id"),
            "merchant_id": payload.get("merchant_id"),
            "environment": payload.get("environment"),
        }
        if (
            not isinstance(payload_scope, dict)
            or dict(scope) != expected
            or payload_scope != expected
        ):
            raise ScopeMismatch("trusted scope does not match payload")
        raw_amount = payload.get("amount_vnd")
        if isinstance(raw_amount, bool) or not isinstance(raw_amount, int):
            raise ValueError("amount_vnd must be an integer")
        settled = SettledPayment(
            event_id=event.event_id,
            tenant_id=_required_string(payload, "tenant_id"),
            merchant_id=_required_uuid(payload, "merchant_id"),
            environment=Environment(_required_string(payload, "environment")),
            intent_id=_required_uuid(payload, "intent_id"),
            settlement_id=_required_uuid(payload, "settlement_id"),
            transaction_id=_required_uuid(payload, "transaction_id"),
            amount_vnd=raw_amount,
            settled_at=datetime.fromisoformat(_required_string(payload, "settled_at")),
            origin=_required_string(payload, "origin"),
            host_ref_type=_required_string(payload, "host_ref_type"),
            host_ref_id=_required_string(payload, "host_ref_id"),
        )
        await self._record.execute(settled)


def _required_string(payload: Any, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _required_uuid(payload: Any, key: str) -> UUID:
    return UUID(_required_string(payload, key))
