"""The host's own outbox consumer: an order is marked paid by ``PaymentSettled``, exactly once.

The outbox delivers at least once; the receipt row keyed by ``event_id`` makes a redelivery a
no-op. The receipt commits only together with exactly one paid order: an event for an order
this host does not have raises, so nothing is recorded and the outbox delivers it again.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from payment_module.ports.publisher import OutboxEventView
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class OrderTables:
    orders: sa.Table
    order_receipts: sa.Table


def define_order_tables(metadata: sa.MetaData, prefix: str = "") -> OrderTables:
    orders = sa.Table(
        f"{prefix}orders",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("paid_by_event_id", sa.Uuid()),
    )
    receipts = sa.Table(
        f"{prefix}order_receipts",
        metadata,
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.String(64), nullable=False),
    )
    return OrderTables(orders, receipts)


class OrderNotFound(LookupError):
    """The event names an order this host does not have (yet); the delivery is retried."""


class OrderConsumer:
    def __init__(self, engine: AsyncEngine, tables: OrderTables) -> None:
        self._engine = engine
        self._tables = tables

    async def publish(self, event: OutboxEventView) -> None:
        if event.event_type != "PaymentSettled":
            return
        orders, receipts = self._tables.orders, self._tables.order_receipts
        order_id = str(event.payload["host_ref_id"])
        async with self._engine.begin() as db:
            receipt = insert(receipts).values(event_id=event.event_id, order_id=order_id)
            first = await db.execute(receipt.on_conflict_do_nothing().returning(sa.literal(1)))
            if first.first() is not None:
                paid = sa.update(orders).where(orders.c.id == order_id)
                result = await db.execute(paid.values(paid_by_event_id=event.event_id))
                if result.rowcount != 1:
                    raise OrderNotFound(order_id)  # rolls the receipt back
