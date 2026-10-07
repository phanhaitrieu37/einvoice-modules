from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables


def violated_constraint(error: IntegrityError) -> str | None:
    cause = error.orig.__cause__ if error.orig is not None else None
    name = getattr(cause, "constraint_name", None)
    return name.split("_", 1)[1] if name else None


class Rows:
    def __init__(self, conn: AsyncConnection, tables: InvoiceTables) -> None:
        self.conn, self.tables = conn, tables
        self.counter = 0

    def _id(self) -> uuid.UUID:
        return uuid.uuid4()

    def _next(self) -> int:
        self.counter += 1
        return self.counter

    async def insert(self, table: sa.Table, values: dict[str, Any]) -> dict[str, Any]:
        async with self.conn.begin_nested():
            await self.conn.execute(table.insert().values(**values))
        return values

    async def expect_violation(
        self, constraint_suffix: str, table: sa.Table, **values: Any
    ) -> None:
        with pytest.raises(IntegrityError) as caught:
            await self.insert(table, values)
        constraint = violated_constraint(caught.value)
        expected = constraint_suffix.removeprefix(self.tables.prefix)
        assert constraint == expected, f"expected constraint {expected}, got {constraint!r}"

    def issuer_account_row(self, **over: Any) -> dict[str, Any]:
        return {
            "id": self._id(),
            "tenant_id": "tenant-test",
            "merchant_id": self._id(),
            "environment": "test",
            "provider": "sepay_einvoice",
            "provider_account_id": "acct-test",
            "template_code": "1",
            "invoice_series_template": "TEST",
            "payment_method": "TM",
            "credential_ref": "env:TEST_CREDENTIAL",
            "buyer_fallback_name": "TEST-FALLBACK-NAME",
            "buyer_fallback_address": "TEST-FALLBACK-ADDRESS",
            "status": "draft",
        } | over

    async def issuer_account(self, **over: Any) -> dict[str, Any]:
        return await self.insert(self.tables.issuer_accounts, self.issuer_account_row(**over))

    def draft_row(self, **over: Any) -> dict[str, Any]:
        number = self._next()
        return {
            "id": self._id(),
            "tenant_id": "tenant-test",
            "merchant_id": self._id(),
            "host_ref_type": "order",
            "host_ref_id": f"order-{number}",
            "buyer": None,
            "items": [],
            "declared_total_vnd": 100,
            "computed_total_vnd": 100,
            "delivery_mode": "auto",
            "revision": 1,
            "status": "open",
        } | over

    async def draft(self, **over: Any) -> dict[str, Any]:
        return await self.insert(self.tables.invoice_drafts, self.draft_row(**over))

    def document_row(self, **over: Any) -> dict[str, Any]:
        number = self._next()
        return {
            "id": self._id(),
            "tenant_id": "tenant-test",
            "merchant_id": self._id(),
            "environment": "test",
            "kind": "original",
            "settlement_id": self._id(),
            "intent_id": self._id(),
            "host_ref_type": "order",
            "host_ref_id": f"order-{number}",
            "reference_code": str(self._id()),
            "settled_amount_vnd": 100,
            "settled_at": sa.func.now(),
            "delivery_mode": "auto",
            "status": "needs_draft",
        } | over

    async def document(self, **over: Any) -> dict[str, Any]:
        return await self.insert(self.tables.documents, self.document_row(**over))

    def receipt_row(self, **over: Any) -> dict[str, Any]:
        return {
            "event_id": self._id(),
            "tenant_id": "tenant-test",
            "merchant_id": self._id(),
            "environment": "test",
            "settlement_id": self._id(),
            "intent_id": self._id(),
            "transaction_id": self._id(),
            "host_ref_type": "order",
            "host_ref_id": "order-1",
            "amount_vnd": 100,
            "origin": "auto",
            "settled_at": sa.func.now(),
            "document_id": self._id(),
        } | over

    async def receipt(self, **over: Any) -> dict[str, Any]:
        return await self.insert(self.tables.settlement_receipts, self.receipt_row(**over))

    def review_row(self, **over: Any) -> dict[str, Any]:
        return {
            "id": self._id(),
            "tenant_id": "tenant-test",
            "document_id": self._id(),
            "reason": "DRAFT_MISSING",
            "details": {},
            "status": "open",
        } | over

    async def review(self, **over: Any) -> dict[str, Any]:
        return await self.insert(self.tables.review_cases, self.review_row(**over))
