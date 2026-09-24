from collections.abc import Mapping
from typing import Protocol


class MetricsSink(Protocol):
    def increment(self, name: str, tags: Mapping[str, str]) -> None: ...


class NoOpMetricsSink:
    def increment(self, name: str, tags: Mapping[str, str]) -> None:
        return None


def increment_safely(sink: MetricsSink, name: str, tags: Mapping[str, str]) -> None:
    try:
        sink.increment(name, tags)
    except Exception:
        return None
