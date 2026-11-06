"""``serve`` and ``e2e`` of the example host, offline: the fake provider is injected, every
secret is synthetic and nothing leaves 127.0.0.1."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from examples.invoice_host import e2e, serve
from examples.invoice_host.main import (
    ACCOUNT_NUMBER,
    ORDER,
    TENANT,
    build,
    define_host_tables,
    place_order,
)
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import unique_prefix
from tests.contract.sepay_einvoice.conftest import (
    all_phases,
    make_account,
    timeout_of,
    token_response,
)
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider

from invoice_module.application.config import InvoiceModuleConfig, today_vn
from invoice_module.domain.enums import DeliveryMode
from invoice_module.ports.clock import SystemClock
from invoice_module.ports.provider import ClientCredentials

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ROOT = Path(__file__).parents[3]
# Shaped like the SePay Test receiving account, not the example's default Vietcombank one.
BANK_CODE, BANK_ACCOUNT = "ACB", "12345678"
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
INV_ENV = {
    "SEPAY_EINVOICE_ENVIRONMENT": "test",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_ID": "synthetic-client",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET": "synthetic-secret",
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


def fake_provider() -> FakeEInvoiceProvider:
    provider = FakeEInvoiceProvider(SystemClock())
    provider.set_account_facts(series=series())
    return provider


def env_file(path: Path, values: dict[str, str]) -> Path:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)
    return path


@pytest.fixture
def setup(pg_url, tmp_path):
    """Synthetic env files, configuration file and serve options under a fresh table prefix."""
    secret = secrets.token_hex(32)
    pm_env = env_file(tmp_path / "pm.env", {serve.PM_WEBHOOK_SECRET: secret})
    inv_env = env_file(tmp_path / "inv.env", INV_ENV)
    config = tmp_path / "accounting-config.json"
    config.write_text(json.dumps(ISSUER_CONFIG))
    prefix = unique_prefix()
    options = serve.ServeOptions(
        database_url=pg_url,
        issuer_config=ISSUER_CONFIG,
        artifact_root=tmp_path / "artifacts",
        pid_file=tmp_path / "p5" / "serve.pid",
        bank_code=BANK_CODE,
        account_number=BANK_ACCOUNT,
        port=free_port(),
        provider_account_id="synthetic-account",
        table_prefix=prefix,
    )
    environ = serve.sandbox_environ({serve.PM_WEBHOOK_SECRET: secret}, INV_ENV)
    return {
        "secret": secret,
        "pm_env": pm_env,
        "inv_env": inv_env,
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


@pytest.fixture
async def engine(pg_url):
    engine = create_async_engine(pg_url)
    yield engine
    await engine.dispose()


async def started(task: asyncio.Task, out: io.StringIO) -> str:
    """The locator ``serve`` prints once it listens."""
    async with asyncio.timeout(30):
        while "READY" not in out.getvalue():
            assert not task.done(), task
            await asyncio.sleep(0.02)
    [locator] = re.findall(r"^READY locator=(\S+)$", out.getvalue(), re.M)
    return locator


async def command(capsys, *argv: str) -> tuple[int, str]:
    capsys.readouterr()
    code = await e2e.run(argv)
    return code, capsys.readouterr().out


async def until(check, timeout_s: float = 30):
    async with asyncio.timeout(timeout_s):
        while not (found := await check()):
            await asyncio.sleep(0.05)
    return found


async def test_serve_issues_a_signed_local_order_and_wait_writes_its_evidence(
    setup, drop_tables, engine, capsys, tmp_path, monkeypatch
):
    options, common = setup["options"], setup["common"]
    tables = define_host_tables(options.table_prefix)
    bound = []

    def listen_socket(port):
        sock = serve_listen_socket(port)
        bound.append(sock.getsockname())
        return sock

    serve_listen_socket = serve.listen_socket
    monkeypatch.setattr(serve, "listen_socket", listen_socket)
    provider = fake_provider()
    stop, out = asyncio.Event(), io.StringIO()
    task = asyncio.create_task(serve.run(options, provider, setup["environ"], stop=stop, out=out))
    try:
        locator = await started(task, out)
        assert bound == [("127.0.0.1", options.port)]
        assert options.pid_file.read_text().strip() == str(os.getpid())
        base = f"http://127.0.0.1:{options.port}"
        async with httpx.AsyncClient() as client:
            health = await client.get(f"{base}/healthz")
        assert (health.status_code, health.json()) == (200, {"status": "ok"})

        order_args = ["--amount", "110000", "--accounting-config", str(setup["config"])]
        before = SystemClock().now()
        code, printed = await command(capsys, "place-order", *common, *order_args)
        assert code == 0
        placed = json.loads(printed)
        assert placed["amount_vnd"] == 110_000 and placed["payment_reference"]
        after = SystemClock().now()
        # The intent's window is 30 minutes from the moment place-order made it.
        expires_at = datetime.fromisoformat(placed["expires_at"])
        assert before + timedelta(minutes=30) <= expires_at <= after + timedelta(minutes=30)
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
        [create] = provider.posts()
        assert create.reference_code == written["settlement_id"]
        assert create.payload["items"][0]["unit_price"] == 100_000
        # The key is always written; its value is the document's (empty on the fake provider
        # as it may be on the sandbox).
        documents = tables.invoice.documents
        async with engine.connect() as db:
            stored = await db.scalar(
                sa.select(documents.c.tax_authority_code).where(
                    documents.c.settlement_id == UUID(written["settlement_id"])
                )
            )
        assert written["tax_authority_code"] == stored

        # The same issued order with another expected reference: nothing is written.
        other = tmp_path / "p5" / "other.json"
        code, _ = await command(
            capsys, "wait", *common, *wait, "--out", str(other),
            "--expect-bank-reference", "LOCAL-NONE",
        )  # fmt: skip
        assert (code, other.exists()) == (4, False)

        # An order paid by a transfer whose reference does not start with LOCAL-.
        code, printed = await command(capsys, "place-order", *common, *order_args)
        paid = json.loads(printed)
        raw, headers, bank_ref = e2e.signed_transfer(
            setup["secret"], BANK_CODE, BANK_ACCOUNT, paid["amount_vnd"],
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

        # A transfer into another account (the example's default one) settles nothing.
        code, printed = await command(capsys, "place-order", *common, *order_args)
        elsewhere = json.loads(printed)
        raw, headers, _ = e2e.signed_transfer(
            setup["secret"], "VCB", ACCOUNT_NUMBER, elsewhere["amount_vnd"],
            elsewhere["payment_reference"],
        )  # fmt: skip
        async with httpx.AsyncClient() as client:
            posted = await client.post(
                f"{base}/webhooks/sepay/{locator}", content=raw, headers=headers
            )
        assert posted.status_code == 200
        inbox, settlements = tables.payment.webhook_inbox, tables.payment.settlements

        async def processed():
            async with engine.connect() as db:
                return await db.scalar(
                    sa.select(inbox.c.status).where(
                        inbox.c.body_sha256 == hashlib.sha256(raw).hexdigest(),
                        inbox.c.status == "processed",
                    )
                )

        await until(processed)

        async def settled(intent_id: str) -> int:
            async with engine.connect() as db:
                return await db.scalar(
                    sa.select(sa.func.count()).where(settlements.c.intent_id == UUID(intent_id))
                )

        assert (await settled(elsewhere["intent_id"]), await settled(paid["intent_id"])) == (0, 1)

        # An order nobody paid times out without a file; --timeout is in seconds.
        code, printed = await command(capsys, "place-order", *common, *order_args)
        unpaid = json.loads(printed)
        never = tmp_path / "p5" / "never.json"
        clock = time.monotonic()
        code, _ = await command(
            capsys, "wait", *common, "--intent-id", unpaid["intent_id"], "--timeout", "2",
            "--interval", "0.2", "--out", str(never), "--expect-bank-reference", "LOCAL-NONE",
        )  # fmt: skip
        assert 2 <= time.monotonic() - clock < 2.6
        assert (code, never.exists()) == (3, False)

        # The host refusing the transfer (a wrong secret: HTTP 401) is a failed pay-local.
        wrong = env_file(tmp_path / "wrong.env", {serve.PM_WEBHOOK_SECRET: "0" * 64})
        capsys.readouterr()
        code = await e2e.run(
            ["pay-local", *common, "--pm-env", str(wrong), "--url", base,
             "--intent-id", unpaid["intent_id"]]
        )  # fmt: skip
        captured = capsys.readouterr()
        assert (code, captured.out) == (2, "")
        assert "HTTP 401" in captured.err
    finally:
        stop.set()
        assert await asyncio.wait_for(task, 30) == 0
    assert not options.pid_file.exists()
    assert port_is_free(options.port)


async def test_wait_counts_operator_steps_and_reads_the_receipt_scope_only(
    setup, drop_tables, engine, capsys, tmp_path
):
    options, common = setup["options"], setup["common"]
    tables = define_host_tables(options.table_prefix)
    stop, out = asyncio.Event(), io.StringIO()
    task = asyncio.create_task(
        serve.run(options, fake_provider(), setup["environ"], stop=stop, out=out)
    )
    try:
        await started(task, out)
        host = build(engine, None, tmp_path, tables=tables, environ={})
        order_id = f"manual-{uuid4()}"
        lines = e2e.order_lines(ISSUER_CONFIG["product_lines"][0], 110_000, "SYN-1")
        created = await place_order(
            host, await e2e._onboarded(engine, tables), order_id, lines,
            delivery_mode=DeliveryMode.MANUAL,
        )  # fmt: skip
        intent_id = str(created.intent.id)
        code, printed = await command(
            capsys, "pay-local", *common, "--pm-env", str(setup["pm_env"]),
            "--url", f"http://127.0.0.1:{options.port}", "--intent-id", intent_id,
        )  # fmt: skip
        assert code == 0
        reference = json.loads(printed)["bank_reference"]

        async def awaiting():
            views = await host.invoice.get_document_status.by_host_ref(TENANT, ORDER, order_id)
            return views[0] if views and views[0].status == "awaiting_approval" else None

        view = await until(awaiting)
        await host.invoice.approve_draft_invoice.execute(view.document_id, "accountant@shop")
        evidence = tmp_path / "manual.json"
        argv = ["wait", *common, "--intent-id", intent_id, "--timeout", "60", "--interval", "0.1"]
        argv += ["--out", str(evidence), "--expect-bank-reference", reference]
        assert (await command(capsys, *argv))[0] == 0
        assert json.loads(evidence.read_text())["manual_invoice_steps"] == 1

        reviews = tables.invoice.review_cases
        async with engine.begin() as db:
            await db.execute(
                sa.insert(reviews).values(
                    id=uuid4(), tenant_id=TENANT, document_id=view.document_id,
                    reason="PROVIDER_TIMEOUT", details={}, status="open",
                )
            )  # fmt: skip
        assert (await command(capsys, *argv))[0] == 0
        assert json.loads(evidence.read_text())["manual_invoice_steps"] == 2

        # A transaction with the same id-free facts but another tenant or environment than
        # the receipt is not the receipt's provenance.
        receipts = tables.invoice.settlement_receipts
        transactions = tables.payment.provider_transactions
        async with engine.connect() as db:
            receipt = (
                await db.execute(
                    sa.select(receipts.c.event_id, receipts.c.transaction_id).where(
                        receipts.c.intent_id == UUID(intent_id)
                    )
                )
            ).one()
            original = (
                await db.execute(
                    sa.select(transactions).where(transactions.c.id == receipt.transaction_id)
                )
            ).one()
        for scope in ({"tenant_id": "another-tenant"}, {"environment": "live"}):
            planted = original._asdict() | scope | {
                "id": uuid4(), "merchant_id": None, "receiving_account_id": None,
                "match_state": "recorded",
            }  # fmt: skip
            async with engine.begin() as db:
                await db.execute(sa.insert(transactions).values(**planted))
                await db.execute(
                    sa.update(receipts)
                    .where(receipts.c.event_id == receipt.event_id)
                    .values(transaction_id=planted["id"])
                )
            evidence.unlink(missing_ok=True)
            assert (await command(capsys, *argv))[0] == 4, scope
            assert not evidence.exists()
        async with engine.begin() as db:
            await db.execute(
                sa.update(receipts)
                .where(receipts.c.event_id == receipt.event_id)
                .values(transaction_id=receipt.transaction_id)
            )
        assert (await command(capsys, *argv))[0] == 0
    finally:
        stop.set()
        assert await asyncio.wait_for(task, 30) == 0


async def test_serve_reuses_its_onboarding_and_refuses_a_live_pid_file(setup, drop_tables):
    options = setup["options"]
    provider = fake_provider()
    locators = []
    for _ in range(2):
        stop, out = asyncio.Event(), io.StringIO()
        task = asyncio.create_task(
            serve.run(options, provider, setup["environ"], stop=stop, out=out)
        )
        locators.append(await started(task, out))
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


async def test_serve_overwrites_a_pid_file_left_by_a_dead_process(setup, drop_tables):
    options = setup["options"]
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    options.pid_file.parent.mkdir(parents=True)
    options.pid_file.write_text(f"{dead.pid}\n")
    stop, out = asyncio.Event(), io.StringIO()
    task = asyncio.create_task(
        serve.run(options, fake_provider(), setup["environ"], stop=stop, out=out)
    )
    await started(task, out)
    assert options.pid_file.read_text().strip() == str(os.getpid())
    stop.set()
    assert await task == 0
    assert not options.pid_file.exists()


async def test_serve_refuses_a_database_onboarded_otherwise(setup, drop_tables, engine):
    options = setup["options"]
    tables = define_host_tables(options.table_prefix)
    stop, out = asyncio.Event(), io.StringIO()
    task = asyncio.create_task(
        serve.run(options, fake_provider(), setup["environ"], stop=stop, out=out)
    )
    await started(task, out)
    stop.set()
    assert await task == 0

    # The same account typed in lower case or with spaces around is the account onboarded
    # (payment-module stores the bank code upper-cased).
    retyped = {"bank_code": f" {BANK_CODE.lower()} ", "account_number": f" {BANK_ACCOUNT} "}
    stop, out = asyncio.Event(), io.StringIO()
    rerun = serve.ServeOptions(**{**options.__dict__, **retyped})
    task = asyncio.create_task(
        serve.run(rerun, fake_provider(), setup["environ"], stop=stop, out=out)
    )
    await started(task, out)
    stop.set()
    assert await task == 0

    async def refused(**changes) -> str:
        changed = serve.ServeOptions(**{**options.__dict__, **changes})
        with pytest.raises(serve.ServeError) as raised:
            async with asyncio.timeout(30):
                await serve.run(changed, fake_provider(), setup["environ"], stop=asyncio.Event())
        assert not options.pid_file.exists()
        return str(raised.value)

    assert "not onboarded for this receiving account" in await refused(bank_code="VCB")
    assert "not onboarded for this receiving account" in await refused(
        account_number=ACCOUNT_NUMBER
    )
    issuers = tables.invoice.issuer_accounts
    async with engine.begin() as db:
        await db.execute(sa.update(issuers).values(status="disabled"))
    assert "half-onboarded" in await refused()
    async with engine.begin() as db:
        await db.execute(sa.delete(issuers))
    assert "half-onboarded" in await refused()


SERVE_MAIN_WITH_FAKE_PROVIDER = """
import json, os, sys
import httpx
from examples.invoice_host import serve
from tests.fakes.fake_einvoice_provider import FakeEInvoiceProvider
from invoice_module.ports.clock import SystemClock

given = json.loads(os.environ["SYNTHETIC_SERVE"])
provider = FakeEInvoiceProvider(SystemClock())
provider.set_account_facts(series=tuple(given["series"]))
serve.sandbox_provider = lambda environ, *args, **kwargs: (provider, httpx.AsyncClient())
sys.exit(serve.main(given["argv"]))
"""


def serve_argv(setup, port: int) -> list[str]:
    options = setup["options"]
    return [
        "--pm-env", str(setup["pm_env"]), "--inv-env", str(setup["inv_env"]),
        "--database-url", options.database_url, "--port", str(port),
        "--bank-code", BANK_CODE, "--account-number", BANK_ACCOUNT,
        "--artifact-root", str(options.artifact_root), "--pid-file", str(options.pid_file),
        "--accounting-config", str(setup["config"]),
        "--provider-account-id", "synthetic-account", "--table-prefix", options.table_prefix,
    ]  # fmt: skip


async def start_serve_main(argv: list[str]) -> asyncio.subprocess.Process:
    env = {
        **os.environ,
        "SYNTHETIC_SERVE": json.dumps({"series": series(), "argv": argv}),
        "PYTHONPATH": str(ROOT),
    }
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", SERVE_MAIN_WITH_FAKE_PROVIDER,
        cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip


async def test_serve_main_wires_its_options_and_sigterm_frees_the_port_and_pid_file(
    setup, drop_tables, engine
):
    options = setup["options"]
    port = free_port()
    process = await start_serve_main(serve_argv(setup, port))
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        assert line.decode().startswith("READY locator="), line
        assert options.pid_file.read_text().strip() == str(process.pid)
        async with httpx.AsyncClient() as client:
            health = await client.get(f"http://127.0.0.1:{port}/healthz")
        assert health.status_code == 200
        payment = define_host_tables(options.table_prefix).payment
        accounts, connections = payment.receiving_accounts, payment.provider_connections
        async with engine.connect() as db:
            onboarded = (
                await db.execute(sa.select(accounts.c.bank_code, accounts.c.account_number))
            ).all()
            secret_refs = (await db.execute(sa.select(connections.c.secret_ref))).scalars().all()
        assert [tuple(row) for row in onboarded] == [(BANK_CODE, BANK_ACCOUNT)]
        assert secret_refs == ["env:SEPAY_TEST_WEBHOOK_SECRET"]
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 30) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    stderr = (await process.stderr.read()).decode()
    assert setup["secret"] not in stderr
    assert not options.pid_file.exists()
    assert port_is_free(port)


async def test_serve_main_exits_1_when_the_port_is_taken(setup, drop_tables):
    options = setup["options"]
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        process = await start_serve_main(serve_argv(setup, port))
        out, err = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 1
    assert out == b""
    assert err.decode().startswith(f"serve: error: could not listen on 127.0.0.1:{port} ")
    assert "Traceback" not in err.decode()
    assert not options.pid_file.exists()


def test_listen_socket_binds_the_loopback_address_only():
    port = free_port()
    with serve.listen_socket(port) as sock:
        assert sock.getsockname() == ("127.0.0.1", port)
        with pytest.raises(serve.ServeError, match=f"could not listen on 127.0.0.1:{port}"):
            serve.listen_socket(port)


def sandbox_answer(requests: list[httpx.Request]):
    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/token":
            return token_response()
        if request.url.path.endswith("/download"):
            kind = request.url.params["type"]
            content = base64.b64encode(b"%PDF-1.4 synthetic" if kind == "pdf" else b"<HDon/>")
            data = {"file_type": kind, "content": content.decode()}
            return httpx.Response(200, json={"success": True, "data": data})
        return httpx.Response(200, json={"success": True, "data": {"items": []}})

    return answer


async def download_both(provider) -> None:
    creds = ClientCredentials("synthetic-client", "synthetic-secret")
    await provider.fetch_artifacts(make_account(), creds, "synthetic-reference", "unused")


async def test_sandbox_provider_waits_no_longer_than_the_module_http_timeout(tmp_path):
    requests = []
    answer = sandbox_answer(requests)

    environ = serve.sandbox_environ({serve.PM_WEBHOOK_SECRET: "synthetic"}, INV_ENV)
    provider, http = serve.sandbox_provider(environ, transport=httpx.MockTransport(answer))
    host = build(
        create_async_engine("postgresql+asyncpg://synthetic@127.0.0.1:1/none"),
        provider,
        tmp_path,
        environ=environ,
    )
    try:
        with pytest.raises(serve.ServeError, match="no sandbox provider account"):
            await serve.discover_provider_account(
                provider, ISSUER_CONFIG, environ, serve.SANDBOX_CREDENTIAL_REF
            )
        await download_both(provider)
    finally:
        await http.aclose()
    config = host.invoice.config
    assert (config.http_timeout_s, config.artifact_timeout_s) == (20.0, 90.0)
    assert [request.url.path for request in requests] == [
        "/v1/token",
        "/v1/provider-accounts",
        "/v1/token",
        *["/v1/invoices/synthetic-reference/download"] * 2,
    ]
    for request in requests[:3]:
        assert timeout_of(request) == all_phases(config.http_timeout_s)
    for request in requests[3:]:
        assert timeout_of(request) == all_phases(config.artifact_timeout_s)


async def test_sandbox_provider_takes_both_timeouts_from_the_given_config():
    requests = []
    environ = serve.sandbox_environ({serve.PM_WEBHOOK_SECRET: "synthetic"}, INV_ENV)
    config = InvoiceModuleConfig(http_timeout_s=7.0, artifact_timeout_s=11.0)
    transport = httpx.MockTransport(sandbox_answer(requests))
    provider, http = serve.sandbox_provider(environ, config, transport=transport)
    try:
        await download_both(provider)
    finally:
        await http.aclose()
    assert [timeout_of(request) for request in requests] == [
        all_phases(7.0),
        all_phases(11.0),
        all_phases(11.0),
    ]


def test_serve_command_line_defaults_and_refusals(tmp_path, capsys):
    required = ["--pm-env", "pm.env", "--inv-env", "inv.env", "--database-url", "postgresql://x"]
    required += ["--artifact-root", "a", "--pid-file", "p"]
    account = ["--bank-code", BANK_CODE, "--account-number", BANK_ACCOUNT]
    args = serve.parse_args(required + account)
    assert (args.port, args.table_prefix, args.provider_account_id) == (8787, "", None)
    assert (args.bank_code, args.account_number) == (BANK_CODE, BANK_ACCOUNT)
    for at, missing in ((0, "--bank-code"), (2, "--account-number")):
        with pytest.raises(SystemExit) as raised:
            serve.parse_args(required + account[:at] + account[at + 2 :])
        assert raised.value.code == 2
        assert f"the following arguments are required: {missing}" in capsys.readouterr().err
    for inv_env in ({"SEPAY_EINVOICE_ENVIRONMENT": "live"}, {}):
        with pytest.raises(serve.ServeError, match="SEPAY_EINVOICE_ENVIRONMENT=test"):
            serve.sandbox_environ({serve.PM_WEBHOOK_SECRET: "x"}, inv_env)
    with pytest.raises(serve.ServeError, match=serve.PM_WEBHOOK_SECRET):
        serve.sandbox_environ({}, {"SEPAY_EINVOICE_ENVIRONMENT": "test"})
    environ = serve.sandbox_environ(
        {serve.PM_WEBHOOK_SECRET: "synthetic", "SEPAY_TEST_API_TOKEN": "unused"},
        {"SEPAY_EINVOICE_ENVIRONMENT": "test"},
    )
    assert environ == {
        "SEPAY_EINVOICE_ENVIRONMENT": "test",
        "SEPAY_TEST_WEBHOOK_SECRET": "synthetic",
    }
    empty = tmp_path / "config.json"
    empty.write_text(json.dumps(ISSUER_CONFIG | {"product_lines": []}))
    with pytest.raises(serve.ServeError, match="no product line"):
        serve.read_issuer_config(empty)


def test_accounting_config_has_no_default(tmp_path, monkeypatch):
    monkeypatch.delenv(serve.ACCOUNTING_CONFIG_ENV, raising=False)
    with pytest.raises(
        serve.ServeError, match="--accounting-config or set INVOICE_ACCOUNTING_CONFIG"
    ):
        serve.accounting_config_path(None)
    named, given = tmp_path / "named.json", tmp_path / "given.json"
    monkeypatch.setenv(serve.ACCOUNTING_CONFIG_ENV, str(named))
    assert serve.accounting_config_path(None) == named
    assert serve.accounting_config_path(given) == given
    with pytest.raises(serve.ServeError, match="could not read the accounting configuration"):
        serve.read_issuer_config(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(serve.ServeError, match="not valid JSON"):
        serve.read_issuer_config(broken)


async def test_commands_without_accounting_config_refuse_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv(serve.ACCOUNTING_CONFIG_ENV, raising=False)
    # The env files do not exist: the configuration is refused before any of them is read.
    argv = ["--pm-env", str(tmp_path / "pm.env"), "--inv-env", str(tmp_path / "inv.env")]
    argv += ["--database-url", "postgresql://x", "--artifact-root", str(tmp_path / "a")]
    argv += ["--pid-file", str(tmp_path / "p"), "--bank-code", BANK_CODE]
    argv += ["--account-number", BANK_ACCOUNT]
    assert await asyncio.to_thread(serve.main, argv) == 1
    refusal = (
        "the accounting configuration is required: pass --accounting-config or set "
        "INVOICE_ACCOUNTING_CONFIG (keys: accounting-config.example.json)"
    )
    assert capsys.readouterr().err == f"serve: error: {refusal}\n"
    order = ["place-order", "--database-url", "postgresql://x", "--amount", "110000"]
    assert await e2e.run(order) == e2e.EXIT_USAGE
    assert capsys.readouterr().err == f"e2e place-order: error: {refusal}\n"


def test_example_accounting_config_has_every_key_the_host_reads():
    config = serve.read_issuer_config(serve.ACCOUNTING_CONFIG_EXAMPLE)
    assert set(ISSUER_CONFIG) - {"rounding_rule"} <= set(config)
    [line] = e2e.order_lines(config["product_lines"][0], 110_000, "SYN-1")
    assert line["unit_price_vnd"] == 100_000


TYPED_URL = "postgresql+asyncpg://synuser:SYNTHPW0MARKER@127.0.0.1:5433/postgres"


async def test_a_mistyped_option_never_echoes_the_typed_database_url(capsys):
    serve_argv = ["--pm-env", "pm.env", "--inv-env", "inv.env", "--artifact-root", "a"]
    serve_argv += ["--pid-file", "p", "--bank-code", BANK_CODE, "--account-number", "1"]
    for run in (
        lambda: serve.parse_args([*serve_argv, "--database_url", TYPED_URL]),
        lambda: serve.parse_args([*serve_argv, "--database-url", TYPED_URL, "--port", "x"]),
    ):
        with pytest.raises(SystemExit) as raised:
            run()
        assert raised.value.code == 2
        err = capsys.readouterr().err
        assert "SYNTHPW0MARKER" not in err and "error:" in err
    wait = ["wait", "--intent-id", str(uuid4()), "--expect-bank-reference", "LOCAL-X"]
    wait += ["--out", "o.json", "--timeout", "1"]
    for argv in (
        [*wait, "--database_url", TYPED_URL],
        ["place-order", "--database-url", TYPED_URL, "--amount", TYPED_URL],
    ):
        with pytest.raises(SystemExit) as raised:
            await e2e.run(argv)
        assert raised.value.code == 2
        err = capsys.readouterr().err
        assert "SYNTHPW0MARKER" not in err and "error:" in err


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        ("serve", ["--pm-env", "pm.env", "--inv-env", "inv.env", "--database-url", "postgresql://x",
                   "--artifact-root", "a", "--pid-file", "p", "--bank-code", BANK_CODE,
                   "--account-number", BANK_ACCOUNT]),
        ("e2e", ["wait", "--database-url", "postgresql://x", "--intent-id", str(uuid4()),
                 "--expect-bank-reference", "LOCAL-X", "--out", "o.json", "--timeout", "1"]),
    ],
)  # fmt: skip
def test_python_m_parses_its_own_command_line_and_hides_a_typed_userinfo(module, argv, tmp_path):
    """``python -m``: ``main`` reads ``sys.argv`` itself; a token glued to a short option is
    reported as unrecognized without repeating it."""
    env = {name: value for name, value in os.environ.items() if name != "DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", f"examples.invoice_host.{module}", *argv,
         "-xsynuser:SYNTHPW0MARKER@127.0.0.1"],
        cwd=tmp_path, env={**env, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True,
        timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 2
    assert result.stderr.splitlines()[-1].endswith("error: unrecognized arguments: <hidden>")
    for typed in ("SYNTHPW0MARKER", "synuser", "@127.0.0.1"):
        assert typed not in result.stdout + result.stderr


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


def test_exit_codes_are_the_documented_numbers():
    assert (e2e.EXIT_USAGE, e2e.EXIT_TIMEOUT, e2e.EXIT_UNKNOWN_SOURCE) == (2, 3, 4)
