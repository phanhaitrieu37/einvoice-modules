from datetime import datetime

import pytest
from examples.invoice_host.main import SYNTHETIC_LINES, smoke
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider

from invoice_module.application.config import today_vn
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.money import LineInput, compute_total
from invoice_module.ports.clock import SystemClock

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.acceptance]

APPROVER = "accountant-synthetic"


def fake_provider() -> FakeEInvoiceProvider:
    clock = SystemClock()
    provider = FakeEInvoiceProvider(clock)
    provider.set_account_facts(series=(f"C{today_vn(clock.now()).year % 100:02d}TSE",))
    return provider


def settled_amount() -> int:
    return compute_total([LineInput(**line) for line in SYNTHETIC_LINES])


async def test_smoke_with_fake_provider(pg_url, tmp_path, environ, host_tables):
    provider = fake_provider()
    result = await smoke(
        provider, tmp_path / "artifacts", database_url=pg_url, tables=host_tables, environ=environ
    )
    assert result["receipts"] == 1
    assert result["documents_original"] == 1
    assert result["status"] == "issued"
    assert result["total_vnd"] == result["settled_amount_vnd"] == settled_amount() == 110_000
    assert result["pdf_sha256"] and result["xml_sha256"]
    assert result["pdf_bytes_sha256"] == result["pdf_sha256"]
    assert result["order_paid"] is True
    assert result["approved_by"] is None
    [create] = [call for call in provider.calls if call.op == "create"]
    assert create.reference_code == result["settlement_id"]
    assert create.payload["items"][0]["item_code"] == "SYN-ITEM-1"


async def test_smoke_manual_mode_with_fake_provider(pg_url, tmp_path, environ, host_tables):
    provider = fake_provider()
    result = await smoke(
        provider,
        tmp_path / "artifacts",
        delivery_mode=DeliveryMode.MANUAL,
        approver=APPROVER,
        database_url=pg_url,
        tables=host_tables,
        environ=environ,
    )
    settled_at = datetime.fromisoformat(result["settled_at"])
    approved_at = datetime.fromisoformat(result["approved_at"])
    assert result["status_before_approval"] == "awaiting_approval"
    # Nothing reaches the provider between the settlement and the approval.
    assert [call.op for call in provider.calls if settled_at <= call.at < approved_at] == []
    assert [call.op for call in provider.calls if call.op == "create"] == ["create"]
    assert all(call.at >= approved_at for call in provider.calls if call.op != "describe_account")
    assert (result["status"], result["approved_by"]) == ("issued", APPROVER)
    assert result["pdf_sha256"] and result["xml_sha256"]
    assert result["total_vnd"] == result["settled_amount_vnd"] == settled_amount()
    assert (result["receipts"], result["order_paid"]) == (1, True)
