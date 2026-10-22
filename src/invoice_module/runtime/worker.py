"""Background loops of the invoice module: issue, poll (with the overdue sweep) and artifacts.

Nothing starts on import. The host runs ``await InvoiceWorker(module).run(stop_event)`` and sets
``stop_event`` to stop; each loop finishes its current step and exits within one interval. A
failing step is logged and counted and the loop carries on: leases and retry state live in the
database, so a step that died is picked up again by a later one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from invoice_module.builder import InvoiceModule
from invoice_module.ports.metrics import increment_safely

logger = logging.getLogger(__name__)

Step = Callable[[], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class WorkerIntervals:
    """Seconds between two steps of each loop."""

    issue_s: float = 1.0
    poll_s: float = 3.0
    artifacts_s: float = 5.0


class InvoiceWorker:
    def __init__(self, module: InvoiceModule, intervals: WorkerIntervals | None = None) -> None:
        self._module = module
        self._intervals = intervals or WorkerIntervals()

    def loops(self) -> dict[str, tuple[Step, float]]:
        """Loop name -> (one step, interval) for every loop this module supports."""
        module, intervals = self._module, self._intervals
        loops: dict[str, tuple[Step, float]] = {}
        if module.issue_queued is not None:
            loops["issue"] = (module.issue_queued.run_batch, intervals.issue_s)
        if module.poll_pending is not None and module.escalate_overdue is not None:
            poll_pending, escalate_overdue = module.poll_pending, module.escalate_overdue

            async def poll() -> None:
                await escalate_overdue.run(module.clock.now())
                await poll_pending.run_batch()

            loops["poll"] = (poll, intervals.poll_s)
        if module.fetch_artifacts is not None:
            loops["artifacts"] = (module.fetch_artifacts.run_batch, intervals.artifacts_s)
        return loops

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run every loop until ``stop_event`` is set."""
        async with asyncio.TaskGroup() as group:
            for name, (step, interval) in self.loops().items():
                group.create_task(self._loop(name, step, interval, stop_event))

    async def _loop(self, name: str, step: Step, interval: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await step()
            except Exception as exc:
                logger.error(
                    "invoice_worker_loop_failed", extra={"loop": name, "error": type(exc).__name__}
                )
                increment_safely(self._module.metrics, "worker_loop_errors_total", {"loop": name})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
