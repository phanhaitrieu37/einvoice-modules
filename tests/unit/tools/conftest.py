import pytest

import tools.einvoice_probe.client as client_module


class PaceClock:
    """Fake monotonic clock for request pacing; sleeping only advances time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def pace_clock(monkeypatch) -> PaceClock:
    clock = PaceClock()
    monkeypatch.setattr(client_module, "_monotonic", clock.monotonic)
    monkeypatch.setattr(client_module, "_sleep", clock.sleep)
    return clock
