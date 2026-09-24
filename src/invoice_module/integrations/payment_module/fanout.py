from collections.abc import Sequence
from typing import Protocol

from payment_module.ports.publisher import OutboxEventView


class OutboxConsumer(Protocol):
    async def publish(self, event: OutboxEventView) -> None: ...


class FanOutPublisher:
    def __init__(self, consumers: Sequence[OutboxConsumer]) -> None:
        if not consumers:
            raise ValueError("at least one outbox consumer is required")
        self._consumers = tuple(consumers)

    async def publish(self, event: OutboxEventView) -> None:
        for consumer in self._consumers:
            await consumer.publish(event)
