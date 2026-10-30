"""The real SePay adapter under the worker: a check answer holding one unusable number.

The adapter refuses a body with ``NaN``, an infinity or a number of 10**19 or more as an
unclear answer. The worker must keep polling without counting the poll, keep
``transient_error`` as the last poll error, raise the stale alert after fifteen minutes and,
at the pending timeout, open the review with that error. Every response is served by an
``httpx.MockTransport``; nothing leaves the process.
"""

import json
from datetime import timedelta

import httpx
import pytest

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

TOKEN = {"success": True, "data": {"access_token": "synthetic-token", "expires_in": 86400}}


def check_answer(reference: str, number: str) -> bytes:
    """A finished ``Success`` check whose ``total_amount`` is the raw JSON token ``number``."""
    body = {
        "success": True,
        "data": {
            "reference_code": reference,
            "status": "Success",
            "message": "Synthetic",
            "invoice": {
                "reference_code": reference,
                "status": "issued",
                "invoice_number": "00000001",
                "issued_date": "2026-10-21",
                "total_amount": "__NUMBER__",
            },
        },
    }
    return json.dumps(body).replace('"__NUMBER__"', number).encode()


@pytest.mark.parametrize("number", ["NaN", "1e19"])
async def test_stray_unusable_number_repolls_until_stale_alert_and_timeout_review(world, number):
    settled = await world.settle()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/token":
            return httpx.Response(200, json=TOKEN)
        if request.method == "POST" and request.url.path == "/v1/invoices/create":
            accepted = {"tracking_code": json.loads(request.content)["reference_code"]}
            return httpx.Response(200, json={"success": True, "data": accepted})
        if request.url.path == f"/v1/invoices/create/check/{settled.reference}":
            return httpx.Response(200, content=check_answer(settled.reference, number))
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SePayEInvoiceClient(http, world.credentials, world.clock, ProcessRateLimiter(1000.0))
    world.provider = SePayEInvoiceProvider(client)
    world.restart()
    try:
        await world.issue()
        doc = await world.doc(settled.document_id)
        assert (doc.status, doc.tracking_code) == ("pending_provider", settled.reference)
        pending_since = doc.pending_since

        async def poll_at(moment):
            world.clock.set(moment)
            assert await world.poll()
            polled = await world.doc(settled.document_id)
            assert polled.status == "pending_provider"
            assert (polled.poll_count, polled.last_error_code) == (0, "transient_error")
            assert polled.invoice_number is None
            return polled

        for _ in range(3):
            doc = await poll_at(doc.next_attempt_at)
        doc = await poll_at(pending_since + timedelta(minutes=15, seconds=1))
        report = await world.module.escalate_overdue.run(world.clock.now())
        assert report.stale == [(settled.document_id, "pending_provider")]
        assert report.timed_out == []

        await poll_at(pending_since + world.config.pending_timeout + timedelta(seconds=1))
        report = await world.module.escalate_overdue.run(world.clock.now())
        assert report.timed_out == [settled.document_id]
    finally:
        await http.aclose()

    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "provider_timeout")
    details = await world.reason_details(settled.document_id, "PROVIDER_TIMEOUT")
    assert (details["last_poll_error"], details["poll_count"]) == ("transient_error", 0)
    checks = [path for path in paths if "/create/check/" in path]
    assert len(checks) == 5
    assert world.metrics.count("invoice_poll_transient_total") == 5
    assert [path for path in paths if path.startswith("POST /v1/invoices/create")] == [
        "POST /v1/invoices/create"
    ]
