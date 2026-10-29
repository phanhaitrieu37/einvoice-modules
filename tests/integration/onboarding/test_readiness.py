import json
import logging
from datetime import date
from pathlib import Path

import pytest
from tests.integration.onboarding.world import ACTOR

from invoice_module.application.issuer_accounts import IssuerAccountNotReady
from invoice_module.application.readiness import ReadinessRejected
from invoice_module.domain.enums import Environment, IssuerAccountStatus

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

FIXTURES = Path(__file__).parents[2] / "fixtures" / "readiness"
PASS = FIXTURES / "evidence-pass.example.json"
FAIL = FIXTURES / "evidence-fail.example.json"
INCONCLUSIVE = FIXTURES / "evidence-inconclusive.example.json"
UNEXPLAINED = FIXTURES / "evidence-inconclusive-unexplained.example.json"
ACTIVE = IssuerAccountStatus.ACTIVE


def evidence_file(tmp_path: Path, name: str, **change) -> Path:
    """The passing evidence with top-level keys or scenarios replaced."""
    evidence = json.loads(PASS.read_text(encoding="utf-8"))
    evidence["scenarios"] |= change.pop("scenarios", {})
    evidence |= change
    path = tmp_path / name
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


async def active_account(world, **over):
    world.provider.set_account_facts(active=True)
    return await world.register(**over)


async def readiness(world, account, path):
    return await world.module.record_issuer_readiness.execute(account.id, str(path), ACTOR)


async def evidence_ref(world, account):
    return (await world.row(account.id))["sandbox_evidence_ref"]


async def test_records_evidence_when_s1_to_s4_pass(world):
    account = await active_account(world)
    calls = world.provider_calls()
    result = await readiness(world, account, PASS)
    assert (result.account_id, result.warnings) == (account.id, ())
    assert world.provider_calls() == calls + 1
    assert await evidence_ref(world, account) == str(PASS)


async def test_rejects_missing_file(world, tmp_path):
    account = await active_account(world)
    for path in (tmp_path / "absent.json", tmp_path):
        with pytest.raises(ReadinessRejected) as caught:
            await readiness(world, account, path)
        assert caught.value.reason == "missing_file"
    assert await evidence_ref(world, account) is None


async def test_rejects_live_evidence(world, tmp_path):
    account = await active_account(world)
    not_json = tmp_path / "not-json.json"
    not_json.write_text("environment: test", encoding="utf-8")
    without = tmp_path / "without.json"
    without.write_text(json.dumps({"scenarios": {}}), encoding="utf-8")
    for path in (evidence_file(tmp_path, "live.json", environment="live"), not_json, without):
        with pytest.raises(ReadinessRejected) as caught:
            await readiness(world, account, path)
        assert caught.value.reason == "wrong_environment"
    assert await evidence_ref(world, account) is None


async def test_rejects_failed_scenario(world, tmp_path):
    account = await active_account(world)
    failing = [
        FAIL,
        evidence_file(tmp_path, "s1.json", scenarios={"s1": "INCONCLUSIVE"}),
        evidence_file(tmp_path, "s3.json", scenarios={"s3": "FAIL"}),
        evidence_file(tmp_path, "s4.json", scenarios={"s4": None}),
        evidence_file(
            tmp_path,
            "async.json",
            scenarios={"s3": "INCONCLUSIVE"},
            details={"s3_variant": "async_failed"},
        ),
        evidence_file(
            tmp_path, "list.json", scenarios={"s3": "INCONCLUSIVE"}, details=["not_triggerable"]
        ),
    ]
    no_scenarios = tmp_path / "no-scenarios.json"
    no_scenarios.write_text(json.dumps({"environment": "test"}), encoding="utf-8")
    failing.append(no_scenarios)
    for path in failing:
        with pytest.raises(ReadinessRejected) as caught:
            await readiness(world, account, path)
        assert caught.value.reason == "scenario_not_pass", path.name
    assert await evidence_ref(world, account) is None


@pytest.mark.parametrize("variant", ["not_triggerable", "sync_reject"])
async def test_accepts_inconclusive_s3_when_not_triggerable(world, tmp_path, variant):
    account = await active_account(world)
    path = (
        INCONCLUSIVE
        if variant == "not_triggerable"
        else evidence_file(
            tmp_path, "sync.json", scenarios={"s3": "INCONCLUSIVE"}, details={"s3_variant": variant}
        )
    )
    result = await readiness(world, account, path)
    assert result.warnings == ()
    assert await evidence_ref(world, account) == str(path)


async def test_rejects_inconclusive_s3_without_variant(world):
    account = await active_account(world)
    with pytest.raises(ReadinessRejected) as caught:
        await readiness(world, account, UNEXPLAINED)
    assert caught.value.reason == "scenario_not_pass"
    assert await evidence_ref(world, account) is None


async def test_live_account_activates_after_readiness(world):
    account = await active_account(world, environment=Environment.LIVE)
    status = world.module.set_issuer_account_status
    with pytest.raises(IssuerAccountNotReady):
        await status.execute(account.id, ACTIVE, ACTOR, "go live")
    result = await readiness(world, account, PASS)
    assert result.warnings == ("approval_date_unknown",)
    activated = await status.execute(account.id, ACTIVE, ACTOR, "go live")
    assert (activated.status, activated.sandbox_evidence_ref) == (ACTIVE, str(PASS))
    assert (await world.row(account.id))["status"] == "active"


@pytest.mark.parametrize("active", [False, None])
async def test_rejects_inactive_provider_account(world, active):
    account = await world.register()
    world.provider.set_account_facts(active=active)
    with pytest.raises(ReadinessRejected) as caught:
        await readiness(world, account, PASS)
    assert caught.value.reason == "account_inactive"
    assert await evidence_ref(world, account) is None


@pytest.mark.parametrize(
    ("environment", "approved", "warnings"),
    [
        (Environment.LIVE, date(2026, 10, 30), ("before_tax_approval_date",)),
        (Environment.LIVE, date(2026, 10, 29), ()),
        (Environment.LIVE, None, ("approval_date_unknown",)),
        (Environment.TEST, date(2026, 10, 30), ()),
        (Environment.TEST, None, ()),
    ],
    ids=["live-before", "live-same-day", "live-unknown", "test-before", "test-unknown"],
)
async def test_live_warns_before_tax_approval_date(world, caplog, environment, approved, warnings):
    account = await active_account(world, environment=environment)
    world.provider.set_account_facts(tax_authority_approved_date=approved)
    with caplog.at_level(logging.WARNING, logger="invoice_module.application.readiness"):
        result = await readiness(world, account, PASS)
    assert result.warnings == warnings
    assert await evidence_ref(world, account) == str(PASS)
    logged = [
        record.warning
        for record in caplog.records
        if record.getMessage() == "invoice_issuer_readiness_warning"
    ]
    assert tuple(logged) == warnings
