"""A create accepted under a tracking code other than the reference is logged as its own event,
so the first such answer on Live is visible; the event carries no provider value."""

import logging
from typing import Any

import pytest

from invoice_module.adapters.sepay.client import Response
from invoice_module.adapters.sepay.provider import SePayEInvoiceProvider
from invoice_module.ports.provider import ProviderTransient

REFERENCE = "00000000-0000-4000-8000-00000000c0de"
OTHER_TRACKING = "11111111-2222-4333-8444-555555555555"
EVENT = "sepay_tracking_code_mismatch"


class _AnsweringClient:
    """Answers every request with one response, as the HTTP client would after the send."""

    def __init__(self, response: Response) -> None:
        self.response = response

    async def request(self, method: str, path: str, **kwargs: Any) -> Response:
        return self.response


def provider_answering(tracking_code: str, status: int = 202) -> SePayEInvoiceProvider:
    body = {"success": True, "data": {"tracking_code": tracking_code}}
    client: Any = _AnsweringClient(Response(status, body, {}))
    return SePayEInvoiceProvider(client)


async def create(provider: SePayEInvoiceProvider) -> Any:
    account: Any = None
    creds: Any = None
    return await provider.create(account, creds, {"reference_code": REFERENCE})


async def test_tracking_code_other_than_the_reference_logs_a_mismatch(caplog):
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderTransient):
        await create(provider_answering(OTHER_TRACKING))
    [record] = [record for record in caplog.records if record.getMessage() == EVENT]
    assert record.levelno == logging.WARNING
    assert (record.method, record.path, record.http_status) == ("POST", "/v1/invoices/create", 202)
    logged = " ".join(str(value) for value in record.__dict__.values())
    assert OTHER_TRACKING not in logged and REFERENCE not in logged


async def test_tracking_code_equal_to_the_reference_logs_nothing(caplog):
    with caplog.at_level(logging.DEBUG):
        accepted = await create(provider_answering(REFERENCE))
    assert accepted.tracking_code == REFERENCE
    assert [record for record in caplog.records if record.getMessage() == EVENT] == []
