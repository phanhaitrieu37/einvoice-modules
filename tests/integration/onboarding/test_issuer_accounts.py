import httpx
import pytest
from tests.integration.onboarding.world import ACTOR

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.application.issuer_accounts import (
    IssuerAccountConflict,
    IssuerAccountMismatch,
    IssuerAccountNotReady,
)
from invoice_module.domain.enums import Environment, IssuerAccountStatus
from invoice_module.ports.provider import ProviderConfigError

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ACTIVE = IssuerAccountStatus.ACTIVE


async def test_register_verifies_account_with_provider(world):
    account = await world.register()
    assert world.provider_calls() == 1
    assert world.credentials.refs == ["env:SYNTHETIC_CREDENTIAL"]
    row = await world.row(account.id)
    assert {
        key: row[key]
        for key in (
            "tenant_id", "merchant_id", "environment", "provider", "provider_account_id",
            "template_code", "invoice_series_template", "payment_method", "credential_ref",
            "seller_store_xid", "buyer_fallback_name", "buyer_fallback_address",
            "buyer_fallback_extra", "issue_date_cross_day_action", "status",
            "sandbox_evidence_ref",
        )
    } == {
        "tenant_id": "tenant-test", "merchant_id": account.merchant_id, "environment": "test",
        "provider": "sepay_einvoice", "provider_account_id": "synthetic-account",
        "template_code": "1", "invoice_series_template": "C{YY}TSE", "payment_method": "TM/CK",
        "credential_ref": "env:SYNTHETIC_CREDENTIAL", "seller_store_xid": None,
        "buyer_fallback_name": "Synthetic fallback buyer", "buyer_fallback_address": "",
        "buyer_fallback_extra": None, "issue_date_cross_day_action": "issue", "status": "draft",
        "sandbox_evidence_ref": None,
    }  # fmt: skip
    # The provider must know the account and this year's series (26 on 29 October 2026).
    world.provider.set_account_facts(provider_account_id="another-account")
    with pytest.raises(IssuerAccountMismatch) as caught:
        await world.register()
    assert caught.value.reason == "provider_account_id"
    world.provider.set_account_facts(provider_account_id="synthetic-account", series=("C25TSE",))
    with pytest.raises(IssuerAccountMismatch) as caught:
        await world.register()
    assert caught.value.reason == "invoice_series"
    assert [row["id"] for row in await world.rows()] == [account.id]


async def test_register_rejects_unknown_template(world):
    world.provider.set_account_facts(templates=("2",))
    with pytest.raises(IssuerAccountMismatch) as caught:
        await world.register(template_code="1")
    assert caught.value.reason == "template_code"
    assert world.provider_calls() == 1
    assert await world.rows() == []


async def test_register_idempotent_same_values(world):
    command = world.command()
    first = await world.module.register_issuer_account.execute(command, ACTOR)
    again = await world.module.register_issuer_account.execute(command, "another-operator")
    assert again == first
    assert len(await world.rows()) == 1
    # A replay of stored settings needs no provider call.
    assert world.provider_calls() == 1
    # An account that went active is returned as it is now.
    await world.module.set_issuer_account_status.execute(first.id, ACTIVE, ACTOR, "go")
    after = await world.module.register_issuer_account.execute(command, ACTOR)
    assert (after.id, after.status) == (first.id, ACTIVE)


@pytest.mark.parametrize(
    "change",
    [
        {"payment_method": "CK"},
        {"invoice_series_template": "C{YY}TSF"},
        {"buyer_fallback_address": "Synthetic street"},
        {"buyer_fallback_extra": {"type": "personal"}},
        {"issue_date_cross_day_action": "review"},
        {"credential_ref": "env:OTHER_CREDENTIAL"},
    ],
    ids=["payment_method", "series", "address", "extra", "cross_day", "credential"],
)
async def test_register_conflict_different_values(world, change):
    command = world.command()
    first = await world.module.register_issuer_account.execute(command, ACTOR)
    calls = world.provider_calls()
    changed = world.command(merchant_id=command.merchant_id, **change)
    with pytest.raises(IssuerAccountConflict):
        await world.module.register_issuer_account.execute(changed, ACTOR)
    assert world.provider_calls() == calls
    [row] = await world.rows()
    assert (row["id"], row["payment_method"], row["buyer_fallback_address"]) == (
        first.id,
        "TM/CK",
        "",
    )


async def test_register_persists_fallback_extra(world):
    extra = {"type": "personal", "email": "fallback@example.invalid"}
    account = await world.register(buyer_fallback_extra=extra)
    assert (await world.row(account.id))["buyer_fallback_extra"] == extra
    assert account.buyer_fallback_extra == extra


@pytest.mark.parametrize("extra", [{"bank_account": "1"}, {"type": 1}], ids=["key", "value"])
async def test_register_rejects_unknown_fallback_extra_key(world, extra):
    with pytest.raises(ValueError, match="buyer fallback"):
        await world.register(buyer_fallback_extra=extra)
    assert world.provider_calls() == 0
    assert await world.rows() == []


async def test_activate_requires_cross_day_action(world):
    account = await world.register(issue_date_cross_day_action=None)
    with pytest.raises(IssuerAccountNotReady) as caught:
        await world.module.set_issuer_account_status.execute(account.id, ACTIVE, ACTOR, "go")
    assert caught.value.reason == "issue_date_cross_day_action"
    assert (await world.row(account.id))["status"] == "draft"
    decided = await world.register(issue_date_cross_day_action="review")
    activated = await world.module.set_issuer_account_status.execute(
        decided.id, ACTIVE, ACTOR, "accountant decided"
    )
    assert activated.status == ACTIVE
    assert (await world.row(decided.id))["status"] == "active"
    with pytest.raises(ValueError, match="reason"):
        await world.module.set_issuer_account_status.execute(decided.id, "disabled", ACTOR, " ")
    disabled = await world.module.set_issuer_account_status.execute(
        decided.id, "disabled", ACTOR, "paused"
    )
    assert disabled.status == IssuerAccountStatus.DISABLED


async def test_live_activate_requires_evidence(world):
    account = await world.register(environment=Environment.LIVE)
    with pytest.raises(IssuerAccountNotReady) as caught:
        await world.module.set_issuer_account_status.execute(account.id, ACTIVE, ACTOR, "go")
    assert caught.value.reason == "sandbox_evidence_ref"
    assert (await world.row(account.id))["status"] == "draft"
    async with world.uow_factory() as uow:
        await uow.issuer_accounts.record_evidence(account.id, "evidence.json", world.clock.now())
        await uow.commit()
    activated = await world.module.set_issuer_account_status.execute(
        account.id, ACTIVE, ACTOR, "readiness recorded"
    )
    assert (activated.status, activated.sandbox_evidence_ref) == (ACTIVE, "evidence.json")


async def test_live_register_without_live_url_raises_config_error(world):
    sent: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise AssertionError("no request may leave the process")

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        client = SePayEInvoiceClient(http, world.credentials, world.clock, ProcessRateLimiter())
        module = world.build(SePayEInvoiceProvider(client))
        with pytest.raises(ProviderConfigError):
            await module.register_issuer_account.execute(
                world.command(environment=Environment.LIVE), ACTOR
            )
    assert sent == []
    assert await world.rows() == []


async def test_register_accepts_blank_fallback_address(world):
    account = await world.register(buyer_fallback_address="")
    assert (await world.row(account.id))["buyer_fallback_address"] == ""
    with pytest.raises(ValueError, match="buyer_fallback_address"):
        await world.register(buyer_fallback_address=None)
    for name in ("", "   ", None):
        with pytest.raises(ValueError, match="buyer_fallback_name"):
            await world.register(buyer_fallback_name=name)
    assert world.provider_calls() == 1
    assert len(await world.rows()) == 1


@pytest.mark.parametrize(
    ("payment_method", "accepted"),
    [("tm", False), ("CASH", False), ("", False),
     ("TM", True), ("CK", True), ("TM/CK", True), ("KHAC", True)],
)  # fmt: skip
async def test_register_rejects_unknown_payment_method(world, payment_method, accepted):
    if accepted:
        account = await world.register(payment_method=payment_method)
        assert (await world.row(account.id))["payment_method"] == payment_method
        assert world.provider_calls() == 1
        return
    with pytest.raises(ValueError, match="payment_method"):
        await world.register(payment_method=payment_method)
    assert world.provider_calls() == 0
    assert await world.rows() == []


async def test_register_refuses_a_malformed_credential_ref_before_the_provider(world):
    for ref in ("SYNTHETIC_CREDENTIAL", "env:", "file:/tmp/x"):
        with pytest.raises(ValueError, match="credential_ref"):
            await world.register(credential_ref=ref)
    assert (world.provider_calls(), world.credentials.refs) == (0, [])
