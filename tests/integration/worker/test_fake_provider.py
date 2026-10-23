"""The fake provider reports what a scenario configures, so readiness checks can rely on it."""

from datetime import date
from uuid import uuid4

import pytest
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider
from tests.fakes.recording import FakeClock
from tests.integration.worker.world import START

from invoice_module.domain.enums import Environment
from invoice_module.ports.provider import AccountFacts, ClientCredentials, IssuerAccountView

pytestmark = pytest.mark.integration

ACCOUNT = IssuerAccountView(
    id=uuid4(),
    tenant_id="tenant-test",
    merchant_id=uuid4(),
    environment=Environment.TEST,
    provider_account_id="synthetic-account",
    template_code="1",
    invoice_series_template="C{YY}TSE",
    payment_method="TM/CK",
    seller_store_xid=None,
    credential_ref="env:SYNTHETIC_CREDENTIAL",
    buyer_fallback_name="Synthetic fallback buyer",
    buyer_fallback_address="",
    buyer_fallback_extra=None,
    issue_date_cross_day_action=None,
)
CREDS = ClientCredentials("synthetic-client", "synthetic-secret")


async def test_describe_account_reports_configured_account_facts():
    provider = FakeEInvoiceProvider(FakeClock(START))
    assert await provider.describe_account(ACCOUNT, CREDS) == AccountFacts(
        "synthetic-account", ("1",), ("C26TSE",), active=True
    )
    provider.set_account_facts(active=False, tax_authority_approved_date=date(2026, 9, 1))
    facts = await provider.describe_account(ACCOUNT, CREDS)
    assert (facts.active, facts.tax_authority_approved_date) == (False, date(2026, 9, 1))
    assert (facts.templates, facts.series) == (("1",), ("C26TSE",))
    assert provider.ops() == ["describe_account", "describe_account"]
