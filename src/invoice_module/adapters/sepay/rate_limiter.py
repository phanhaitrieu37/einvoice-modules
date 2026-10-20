"""One token bucket per process for the SePay eInvoice API.

Every issuer account of a process shares one limiter so that a worker never bursts more
requests at the provider than ``rate_per_second`` allows (default 2 req/s).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

type Monotonic = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]


class ProcessRateLimiter:
    """``acquire()`` waits until a request may be sent; burst is ``capacity`` requests."""

    def __init__(
        self,
        rate_per_second: float = 2.0,
        *,
        capacity: float = 1.0,
        monotonic: Monotonic = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate must be positive")
        if capacity < 1.0:
            raise ValueError("capacity must allow at least one request")
        self.rate_per_second = rate_per_second
        self._capacity = capacity
        self._tokens = capacity
        self._monotonic = monotonic
        self._sleep = sleep
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._monotonic()
                elapsed = max(now - self._updated, 0.0)
                self._tokens = min(self._capacity, self._tokens + elapsed * self.rate_per_second)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self.rate_per_second)
