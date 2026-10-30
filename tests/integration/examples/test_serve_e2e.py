"""``serve`` and ``e2e`` of the example host, offline: the fake provider is injected, every
secret is synthetic and nothing leaves 127.0.0.1."""

import asyncio
import io
import json
import os
import re
import secrets
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest
from examples.invoice_host import e2e, serve
from examples.invoice_host.main import ACCOUNT_NUMBER, define_host_tables
from examples.invoice_host.sign_webhook import signed_delivery
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import unique_prefix
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider

from invoice_module.application.config import today_vn
from invoice_module.ports.clock import SystemClock

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ROOT = Path(__file__).parents[3]
ISSUER_CONFIG = {
    "issue_date_cross_day_action": "issue",
    "template_code": "1",
    "invoice_series_template": "C{YY}TSE",
    "payment_method": "TM/CK",
    "buyer_fallback_name": "Synthetic fallback buyer",
    "buyer_fallback_address": "",
    "rounding_rule": "half_up_per_line_tolerance_1",
    "product_lines": [
        {
            "name": "Synthetic service",
            "unit": "month",
            "quantity": 1,
            "unit_price_vnd": 100_000,
            "tax_rate": 10,
        }
    ],
}


def series() -> tuple[str, ...]:
    return (f"C{today_vn(SystemClock().now()).year % 100:02d}TSE",)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


@pytest.fixture
def setup(pg_url, tmp_path):
    """Synthetic env file, configuration file and serve options under a fresh table prefix."""
    secret = secrets.token_hex(32)
    pm_env = tmp_path / "pm.env"
    pm_env.write_text(f"{serve.PM_WEBHOOK_SECRET}={secret}\n")
    pm_env.chmod(0o600)
    config = tmp_path / "accounting-config.json"
    config.write_text(json.dumps(ISSUER_CONFIG))
    prefix = unique_prefix()
    options = serve.ServeOptions(
        database_url=pg_url,
        issuer_config=ISSUER_CONFIG,
        artifact_root=tmp_path / "artifacts",
        pid_file=tmp_path / "p5" / "serve.pid",
        port=free_port(),
        provider_account_id="synthetic-account",
        table_prefix=prefix,
    )
    environ = serve.sandbox_environ(
        {serve.PM_WEBHOOK_SECRET: secret},
        {
            "SEPAY_EINVOICE_ENVIRONMENT": "test",
            "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "synthetic-client",
            "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "synthetic-secret",
        },
    )
    return {
        "secret": secret,
        "pm_env": pm_env,
        "config": config,
        "options": options,
        "environ": environ,
        "common": ["--database-url", pg_url, "--table-prefix", prefix],
    }


@pytest.fixture
async def drop_tables(setup, pg_url):
    yield
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            tables = define_host_tables(setup["options"].table_prefix)
            await conn.run_sync(tables.metadata.drop_all)
    finally:
        await engine.dispose()


async def command(capsys, *argv: str) -> tuple[int, str]:
    capsys.readouterr()
    code = await e2e.run(argv)
    return code, capsys.readouterr().out


async def test_serve_issues_a_signed_local_order_and_wait_writes_its_evidence(
    setup, drop_tables, capsys, tmp_path
):
    options, common = setup["options"], setup["common"]
    provider = FakeEInvoiceProvider(SystemClock())
    provider.set_account_facts(series=series())
    stop, out = asyncio.Event(), io.StringIO()
    task = asyncio.create_task(serve.run(options, provider, setup["environ"], stop=stop, out=out))
    try:
        async with asyncio.timeout(30):
            while "READY" not in out.getvalue():
                assert not task.done(), task
                await asyncio.sleep(0.02)
        [locator] = re.findall(r"^READY locator=(\S+)$", out.getvalue(), re.M)
        assert options.pid_file.read_text().strip() == str(os.getpid())
        base = f"http://127.0.0.1:{options.port}"
        async with httpx.AsyncClient() as client:
            health = await client.get(f"{base}/healthz")
        assert (health.status_code, health.json()) == (200, {"status": "ok"})

        order_args = ["--amount", "110000", "--accounting-config", str(setup["config"])]
        code, printed = await command(capsys, "place-order", *common, *order_args)
        assert code == 0
        placed = json.loads(printed)
        assert placed["amount_vnd"] == 110_000 and placed["payment_reference"]
        code, printed = await command(
            capsys, "pay-local", *common, "--pm-env", str(setup["pm_env"]), "--url", base,
            "--intent-id", placed["intent_id"],
        )  # fmt: skip
        assert code == 0
        local_ref = json.loads(printed)["bank_reference"]
        assert local_ref.startswith("LOCAL-")
        assert setup["secret"] not in printed

        evidence = tmp_path / "p5" / "local-http.json"
        wait = ["--intent-id", placed["intent_id"], "--timeout", "60", "--interval", "0.1"]
        code, _ = await command(
            capsys, "wait", *common, *wait, "--out", str(evidence),
            "--expect-bank-reference", local_ref.lower(),
        )  # fmt: skip
        assert code == 0
        written = json.loads(evidence.read_text())
        assert written["intent_id"] == placed["intent_id"]
        assert written["bank_reference"] == local_ref
        assert (written["status"], written["source"]) == ("issued", "local_signed")
        assert (written["original_documents"], written["manual_invoice_steps"]) == (1, 0)
        assert written["total_equals_settled"] is True
        assert written["invoice_number"] and written["pdf_sha256"] and written["xml_sha256"]
        assert "tax_authority_code" in written
        [create] = provider.posts()
        assert create.reference_code == written["settlement_id"]
        assert create.payload["items"][0]["unit_price"] == 100_000

        # The same issued order with another expected reference: nothing is written.
        other = tmp_path / "p5" / "other.json"
        code, _ = await command(
            capsys, "wait", *common, *wait, "--out", str(other),
            "--expect-bank-reference", "LOCAL-NONE",
        )  # fmt: skip
        assert (code, other.exists()) == (e2e.EXIT_UNKNOWN_SOURCE, False)

        # An order paid by a transfer whose reference does not start with LOCAL-.
        code, printed = await command(capsys, "place-order", *common, *order_args)
        paid = json.loads(printed)
        raw, headers, bank_ref = signed_delivery(
            setup["secret"], ACCOUNT_NUMBER, paid["amount_vnd"], paid["payment_reference"],
            paid["payment_reference"], reference_code_prefix="FT-SYNTHETIC-",
        )  # fmt: skip
        async with httpx.AsyncClient() as client:
            posted = await client.post(
                f"{base}/webhooks/sepay/{locator}", content=raw, headers=headers
            )
        assert posted.status_code == 200
        sepay = tmp_path / "p5" / "sepay.json"
        code, _ = await command(
            capsys, "wait", *common, "--intent-id", paid["intent_id"], "--timeout", "60",
            "--interval", "0.1", "--out", str(sepay), "--expect-bank-reference", f" {bank_ref} ",
        )  # fmt: skip
        assert code == 0
        assert json.loads(sepay.read_text())["source"] == "sepay_test"

        # An order nobody paid times out without a file.
        code, printed = await command(capsys, "place-order", *common, *order_args)
        unpaid = json.loads(printed)
        never = tmp_path / "p5" / "never.json"
        code, _ = await command(
            capsys, "wait", *common, "--intent-id", unpaid["intent_id"], "--timeout", "1",
            "--interval", "0.2", "--out", str(never), "--expect-bank-reference", "LOCAL-NONE",
        )  # fmt: skip
        assert (code, never.exists()) == (e2e.EXIT_TIMEOUT, False)
    finally:
        stop.set()
        assert await asyncio.wait_for(task, 30) == 0
    assert not options.pid_file.exists()
    assert port_is_free(options.port)


async def test_serve_reuses_its_onboarding_and_refuses_a_live_pid_file(setup, drop_tables):
    options = setup["options"]
    provider = FakeEInvoiceProvider(SystemClock())
    provider.set_account_facts(series=series())
    locators = []
    for _ in range(2):
        stop, out = asyncio.Event(), io.StringIO()
        task = asyncio.create_task(
            serve.run(options, provider, setup["environ"], stop=stop, out=out)
        )
        async with asyncio.timeout(30):
            while "READY" not in out.getvalue():
                assert not task.done(), task
                await asyncio.sleep(0.02)
        locators += re.findall(r"READY locator=(\S+)", out.getvalue())
        stop.set()
        assert await task == 0
    assert len(locators) == 2 and locators[0] == locators[1]
    assert [call.op for call in provider.calls] == ["describe_account"]

    options.pid_file.write_text(f"{os.getppid()}\n")
    stop = asyncio.Event()
    task = asyncio.create_task(serve.run(options, provider, setup["environ"], stop=stop))
    await asyncio.wait({task}, timeout=10)
    stop.set()
    with pytest.raises(serve.ServeError, match="running process"):
        await task
    assert options.pid_file.read_text().strip() == str(os.getppid())


SERVE_WITH_FAKE_PROVIDER = """
import asyncio, json, os, sys
from pathlib import Path
from examples.invoice_host import serve
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider
from invoice_module.ports.clock import SystemClock

given = json.loads(os.environ["SYNTHETIC_SERVE"])
provider = FakeEInvoiceProvider(SystemClock())
provider.set_account_facts(series=tuple(given["series"]))
options = serve.ServeOptions(
    database_url=given["database_url"],
    issuer_config=given["issuer_config"],
    artifact_root=Path(given["artifact_root"]),
    pid_file=Path(given["pid_file"]),
    port=given["port"],
    provider_account_id="synthetic-account",
    table_prefix=given["table_prefix"],
)
sys.exit(asyncio.run(serve.run(options, provider, given["environ"])))
"""


async def test_sigterm_stops_serve_frees_the_port_and_deletes_the_pid_file(setup, drop_tables):
    options = setup["options"]
    given = {
        "series": series(),
        "database_url": options.database_url,
        "issuer_config": ISSUER_CONFIG,
        "artifact_root": str(options.artifact_root),
        "pid_file": str(options.pid_file),
        "port": options.port,
        "table_prefix": options.table_prefix,
        "environ": setup["environ"],
    }
    env = {**os.environ, "SYNTHETIC_SERVE": json.dumps(given), "PYTHONPATH": str(ROOT)}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", SERVE_WITH_FAKE_PROVIDER,
        cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        assert line.decode().startswith("READY locator="), line
        assert options.pid_file.read_text().strip() == str(process.pid)
        async with httpx.AsyncClient() as client:
            health = await client.get(f"http://127.0.0.1:{options.port}/healthz")
        assert health.status_code == 200
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 30) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    stderr = (await process.stderr.read()).decode()
    assert setup["secret"] not in stderr
    assert not options.pid_file.exists()
    assert port_is_free(options.port)


def test_serve_command_line_defaults_and_refusals(tmp_path):
    required = ["--pm-env", "pm.env", "--inv-env", "inv.env", "--database-url", "postgresql://x"]
    required += ["--artifact-root", "a", "--pid-file", "p"]
    args = serve.parse_args(required)
    assert (args.port, args.table_prefix, args.provider_account_id) == (8787, "", None)
    with pytest.raises(serve.ServeError, match="SEPAY_EINVOICE_ENVIRONMENT=test"):
        serve.sandbox_environ(
            {serve.PM_WEBHOOK_SECRET: "x"}, {"SEPAY_EINVOICE_ENVIRONMENT": "live"}
        )
    with pytest.raises(serve.ServeError, match=serve.PM_WEBHOOK_SECRET):
        serve.sandbox_environ({}, {"SEPAY_EINVOICE_ENVIRONMENT": "test"})
    environ = serve.sandbox_environ(
        {serve.PM_WEBHOOK_SECRET: "synthetic"}, {"SEPAY_EINVOICE_ENVIRONMENT": "test"}
    )
    assert environ[serve.CONNECTION_SECRET] == "synthetic"
    empty = tmp_path / "config.json"
    empty.write_text(json.dumps(ISSUER_CONFIG | {"product_lines": []}))
    with pytest.raises(serve.ServeError, match="no product line"):
        serve.read_issuer_config(empty)


def test_order_line_total_is_the_amount_with_the_configured_tax():
    product = ISSUER_CONFIG["product_lines"][0]
    [line] = e2e.order_lines(product, 110_000, "SYN-1")
    assert (line["unit_price_vnd"], line["tax_rate"], line["item_code"]) == (100_000, 10, "SYN-1")
    assert (line["name"], line["unit"]) == (product["name"], product["unit"])
    [odd] = e2e.order_lines(product, 2_001, "SYN-1")
    assert odd["unit_price_vnd"] == 1_819  # 1 819 + 182 tax (half up) = 2 001
    with pytest.raises(e2e.UsageError):
        e2e.order_lines(product, 0, "SYN-1")


@pytest.mark.parametrize(
    ("stored", "expected", "source"),
    [
        ("LOCAL-FT0000000001", " local-ft0000000001 ", "local_signed"),
        ("FT26300000001", "ft26300000001", "sepay_test"),
        ("FT26300000001", "FT26300000002", "unknown"),
        (None, "FT26300000001", "unknown"),
        ("  ", "", "unknown"),
    ],
)
def test_source_is_derived_from_the_matching_bank_reference(stored, expected, source):
    assert e2e.source_of(stored, expected) == source
