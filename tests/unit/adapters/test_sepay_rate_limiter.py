import asyncio

import pytest

from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def test_bucket_spaces_requests() -> None:
    time = FakeTime()
    limiter = ProcessRateLimiter(2.0, monotonic=time.monotonic, sleep=time.sleep)
    sent: list[float] = []
    for _ in range(4):
        await limiter.acquire()
        sent.append(time.now)
    assert sent == [100.0, 100.5, 101.0, 101.5]
    assert time.sleeps == [0.5, 0.5, 0.5]
    time.now += 10.0
    await limiter.acquire()
    assert time.sleeps == [0.5, 0.5, 0.5]
    time.now += 0.25
    await limiter.acquire()
    assert time.sleeps == [0.5, 0.5, 0.5, 0.25]


async def test_concurrent_callers_share_the_bucket_in_arrival_order() -> None:
    time = FakeTime()
    limiter = ProcessRateLimiter(2.0, monotonic=time.monotonic, sleep=time.sleep)
    sent: list[tuple[str, float]] = []

    async def call(name: str) -> None:
        await limiter.acquire()
        sent.append((name, time.now))

    await asyncio.gather(call("a"), call("b"), call("c"))
    assert sent == [("a", 100.0), ("b", 100.5), ("c", 101.0)]


@pytest.mark.parametrize(("rate", "capacity"), [(0.0, 1.0), (-1.0, 1.0), (2.0, 0.5)])
def test_invalid_rate_or_capacity_rejected(rate: float, capacity: float) -> None:
    with pytest.raises(ValueError):
        ProcessRateLimiter(rate, capacity=capacity)
