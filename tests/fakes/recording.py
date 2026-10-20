"""Recording test doubles: a settable clock, a metrics sink, credentials and crash injection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from invoice_module.ports.provider import ClientCredentials


class FakeClock:
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware datetime")
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


class RecordingMetrics:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, str]]] = []

    def increment(self, name: str, tags: Mapping[str, str]) -> None:
        self.events.append((name, dict(tags)))

    def count(self, name: str, **tags: str) -> int:
        return sum(
            1
            for event, event_tags in self.events
            if event == name and all(event_tags.get(key) == value for key, value in tags.items())
        )


class StaticCredentials:
    def __init__(self) -> None:
        self.refs: list[str] = []

    async def client_credentials(self, ref: str) -> ClientCredentials:
        self.refs.append(ref)
        return ClientCredentials("synthetic-client", "synthetic-secret")


class Crash(BaseException):
    """A simulated process death; ``BaseException`` so no ``except Exception`` swallows it."""


class CrashableUnitOfWorkFactory:
    """Wraps a unit-of-work factory so a test can kill the process around a commit."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.crash_before_commits = 0
        self.crash_after_commits = 0
        self.commits = 0

    def crash_before_next_commit(self) -> None:
        self.crash_before_commits += 1

    def crash_after_next_commit(self) -> None:
        self.crash_after_commits += 1

    def __call__(self) -> _CrashableUnitOfWork:
        return _CrashableUnitOfWork(self._inner(), self)


class _CrashableUnitOfWork:
    def __init__(self, inner: Any, owner: CrashableUnitOfWorkFactory) -> None:
        self._inner = inner
        self._owner = owner

    async def __aenter__(self) -> _CrashableUnitOfWork:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._inner.__aexit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def commit(self) -> None:
        owner = self._owner
        if owner.crash_before_commits:
            owner.crash_before_commits -= 1
            raise Crash("crash before commit")
        await self._inner.commit()
        owner.commits += 1
        if owner.crash_after_commits:
            owner.crash_after_commits -= 1
            raise Crash("crash after commit")
