"""HTTP side of the example host: the payment-module webhook router behind a real listener.

``create_app`` mounts ``make_webhook_router`` (SePay posts bank transfers to
``/webhooks/sepay/{locator}``) and ``GET /healthz``. ``serve`` runs it with uvicorn on a fixed
local address until a stop event; ``run_workers`` runs the payment and invoice workers side by
side until the same kind of event. Nothing starts on import.
"""

from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI
from payment_module.adapters.fastapi import make_webhook_router
from payment_module.builder import PaymentModule
from payment_module.runtime import PaymentWorker
from payment_module.runtime import WorkerIntervals as PaymentIntervals

from invoice_module.builder import InvoiceModule
from invoice_module.runtime import InvoiceWorker
from invoice_module.runtime import WorkerIntervals as InvoiceIntervals

WEBHOOK_PATH = "/webhooks/sepay/{locator}"
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8765


def create_app(payment_module: PaymentModule, path: str = WEBHOOK_PATH) -> FastAPI:
    app = FastAPI()
    app.include_router(make_webhook_router(payment_module, path=path))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def run_workers(
    payment_module: PaymentModule,
    invoice_module: InvoiceModule,
    stop_event: asyncio.Event,
    *,
    fast: bool = False,
) -> None:
    """Both workers until ``stop_event`` is set; ``fast`` shortens every loop interval for a
    local smoke (the invoice poll schedule itself is unchanged)."""
    payment_intervals = PaymentIntervals(inbox_s=0.05, outbox_s=0.05) if fast else None
    invoice_intervals = (
        InvoiceIntervals(issue_s=0.05, poll_s=0.05, sweep_s=1.0, artifacts_s=0.05) if fast else None
    )
    async with asyncio.TaskGroup() as group:
        group.create_task(PaymentWorker(payment_module, payment_intervals).run(stop_event))
        group.create_task(InvoiceWorker(invoice_module, invoice_intervals).run(stop_event))


async def serve(
    app: FastAPI,
    stop_event: asyncio.Event,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Listen on ``host:port`` until ``stop_event`` is set, then shut the listener down."""
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        await task
