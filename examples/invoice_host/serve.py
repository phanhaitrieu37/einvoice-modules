"""The example host as one long-running process: webhook listener plus both workers.

    python -m examples.invoice_host.serve --pm-env PM_ENV_FILE --inv-env INV_ENV_FILE \\
        --database-url "$DATABASE_URL" --port 8787 \\
        --artifact-root .evidence/p5/artifacts --pid-file .evidence/p5/serve.pid

Both env files are read inside this process and never printed: the payment-module file gives
``SEPAY_TEST_WEBHOOK_SECRET`` (the secret SePay Test signs its webhooks with), the invoice
file gives the sandbox credentials named by ``env:SEPAY_EINVOICE_SANDBOX`` and must declare
``SEPAY_EINVOICE_ENVIRONMENT=test``. ``DATABASE_URL`` names a PostgreSQL that is already
running; this process never starts one. On the first run the tables are created and the
payment side and the sandbox issuer account (from the accountant's configuration file) are
onboarded; a later run reuses them. Once listening on ``127.0.0.1:PORT`` the process prints
``READY locator=<locator>`` (SePay Test posts to ``/webhooks/sepay/<locator>``). SIGTERM or
SIGINT stops the workers and the listener, deletes the pid file and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

import httpx
import sqlalchemy as sa
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from invoice_module.adapters.sepay import SePayEInvoiceClient, SePayEInvoiceProvider
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.application.config import today_vn
from invoice_module.domain.enums import CrossDayAction, Environment
from invoice_module.ports.clock import SystemClock
from invoice_module.ports.provider import EInvoiceProvider, IssuerAccountView
from invoice_module.secrets import EnvCredentialResolver
from tools.einvoice_probe.envfile import load_env_file

from .main import TENANT, HostTables, build, define_host_tables, onboard
from .server import create_app, run_workers

DEFAULT_PORT = 8787
LISTEN_HOST = "127.0.0.1"
SANDBOX_CREDENTIAL_REF = "env:SEPAY_EINVOICE_SANDBOX"
# The payment-module env file names the SePay Test webhook secret this way; ``onboard``
# stores the connection's reference as ``env:SEPAY_WEBHOOK_SECRET``, resolved in-process.
PM_WEBHOOK_SECRET = "SEPAY_TEST_WEBHOOK_SECRET"
CONNECTION_SECRET = "SEPAY_WEBHOOK_SECRET"
SANDBOX_HTTP_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ServeOptions:
    database_url: str
    issuer_config: Mapping[str, Any]
    artifact_root: Path
    pid_file: Path
    port: int = DEFAULT_PORT
    provider_account_id: str | None = None
    credential_ref: str = SANDBOX_CREDENTIAL_REF
    table_prefix: str = ""


class ServeError(RuntimeError):
    """A start-up condition this process refuses; the message holds no secret."""


def default_accounting_config() -> Path:
    """``INVOICE_ACCOUNTING_CONFIG``, else ``plans/reports/accounting-config.json`` of the main
    checkout (the configuration file the accountant's answers were copied into)."""
    override = os.environ.get("INVOICE_ACCOUNTING_CONFIG")
    if override:
        return Path(override).expanduser()
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(common).parent / "plans" / "reports" / "accounting-config.json"


def read_issuer_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not config.get("product_lines"):
        raise ServeError("the accounting configuration has no product line")
    return config


def sandbox_environ(pm_env: Mapping[str, str], inv_env: Mapping[str, str]) -> dict[str, str]:
    """The variables both modules resolve, from the two env files; refuses anything but the
    sandbox."""
    if inv_env.get("SEPAY_EINVOICE_ENVIRONMENT") != "test":
        raise ServeError("the invoice env file must declare SEPAY_EINVOICE_ENVIRONMENT=test")
    if not pm_env.get(PM_WEBHOOK_SECRET):
        raise ServeError(f"the payment env file has no {PM_WEBHOOK_SECRET}")
    return {**inv_env, CONNECTION_SECRET: pm_env[PM_WEBHOOK_SECRET]}


def sandbox_provider(environ: Mapping[str, str]) -> tuple[SePayEInvoiceProvider, httpx.AsyncClient]:
    """The SePay adapter on the sandbox base URL (the client refuses any other for ``test``)."""
    http = httpx.AsyncClient()
    client = SePayEInvoiceClient(
        http,
        EnvCredentialResolver(environ),
        SystemClock(),
        ProcessRateLimiter(2.0),
        http_timeout_s=SANDBOX_HTTP_TIMEOUT_S,
    )
    return SePayEInvoiceProvider(client), http


async def discover_provider_account(
    provider: SePayEInvoiceProvider,
    issuer_config: Mapping[str, Any],
    environ: Mapping[str, str],
    credential_ref: str,
) -> str:
    """The sandbox provider account offering the configured template and this year's series."""
    creds = await EnvCredentialResolver(environ).client_credentials(credential_ref)

    def view(provider_account_id: str) -> IssuerAccountView:
        return IssuerAccountView(
            id=UUID(int=0),
            tenant_id=TENANT,
            merchant_id=UUID(int=0),
            environment=Environment.TEST,
            provider_account_id=provider_account_id,
            template_code=issuer_config["template_code"],
            invoice_series_template=issuer_config["invoice_series_template"],
            payment_method=issuer_config["payment_method"],
            seller_store_xid=None,
            credential_ref=credential_ref,
            buyer_fallback_name=issuer_config["buyer_fallback_name"],
            buyer_fallback_address=issuer_config["buyer_fallback_address"],
            buyer_fallback_extra=None,
            issue_date_cross_day_action=CrossDayAction(
                issuer_config["issue_date_cross_day_action"]
            ),
        )

    year = f"{today_vn(SystemClock().now()).year % 100:02d}"
    series = issuer_config["invoice_series_template"].replace("{YY}", year)
    for provider_account_id in await provider.provider_account_ids(view(""), creds):
        facts = await provider.describe_account(view(provider_account_id), creds)
        if (issuer_config["template_code"], series) in zip(
            facts.templates, facts.series, strict=True
        ):
            return provider_account_id
    raise ServeError("no sandbox provider account offers the configured template and series")


async def ensure_onboarded(
    engine: AsyncEngine,
    tables: HostTables,
    host: Any,
    options: ServeOptions,
    provider_account_id: Callable[[], Any],
) -> str:
    """The webhook locator; onboards both modules the first time."""
    merchants = tables.payment.merchants
    connections = tables.payment.provider_connections
    accounts = tables.invoice.issuer_accounts
    async with engine.connect() as db:
        merchant_id = await db.scalar(
            sa.select(merchants.c.id).where(merchants.c.tenant_id == TENANT)
        )
        if merchant_id is not None:
            locator = await db.scalar(
                sa.select(connections.c.locator).where(connections.c.merchant_id == merchant_id)
            )
            issuer = await db.scalar(
                sa.select(accounts.c.id).where(
                    accounts.c.merchant_id == merchant_id, accounts.c.status == "active"
                )
            )
            if locator is None or issuer is None:
                raise ServeError("the database holds a half-onboarded host; use a fresh database")
            return str(locator)
    onboarded = await onboard(
        host.payment,
        host.invoice,
        options.issuer_config,
        provider_account_id=await provider_account_id(),
        credential_ref=options.credential_ref,
    )
    return str(onboarded.connection.locator)


@contextlib.contextmanager
def pid_file(path: Path) -> Iterator[None]:
    """Write this process's id; refuse when the file names a process that is still alive, and
    delete the file on the way out only while it still holds this process's id."""
    if path.exists():
        try:
            other = int(path.read_text().strip())
        except ValueError:
            other = None
        if other is not None and other != os.getpid() and _alive(other):
            raise ServeError(f"{path} names a running process ({other}); stop it first")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError, ValueError):
            if int(path.read_text().strip()) == os.getpid():
                path.unlink()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def run(
    options: ServeOptions,
    provider: EInvoiceProvider,
    environ: Mapping[str, str],
    *,
    stop: asyncio.Event | None = None,
    out: TextIO = sys.stdout,
    discover: Callable[[], Any] | None = None,
) -> int:
    """Serve until ``stop`` is set (or SIGTERM/SIGINT when no ``stop`` is given)."""
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

    async def provider_account_id() -> str:
        if options.provider_account_id:
            return options.provider_account_id
        if discover is None:
            raise ServeError("--provider-account-id is required with this provider")
        return await discover()

    tables = define_host_tables(options.table_prefix)
    with pid_file(options.pid_file):
        engine = create_async_engine(options.database_url)
        try:
            async with engine.begin() as db:
                await db.run_sync(tables.metadata.create_all)
            host = build(engine, provider, options.artifact_root, tables=tables, environ=environ)
            locator = await ensure_onboarded(engine, tables, host, options, provider_account_id)
            listener = uvicorn.Server(
                uvicorn.Config(
                    create_app(host.payment),
                    host=LISTEN_HOST,
                    port=options.port,
                    log_level="warning",
                )
            )
            serving = asyncio.create_task(listener.serve())
            workers = asyncio.create_task(run_workers(host.payment, host.invoice, stop))
            try:
                while not listener.started and not serving.done():
                    await asyncio.sleep(0.02)
                if not listener.started:
                    raise ServeError(f"could not listen on {LISTEN_HOST}:{options.port}")
                print(f"READY locator={locator}", file=out, flush=True)
                stopped = asyncio.create_task(stop.wait())
                await asyncio.wait({stopped, serving}, return_when=asyncio.FIRST_COMPLETED)
                stopped.cancel()
            finally:
                stop.set()
                listener.should_exit = True
                with contextlib.suppress(SystemExit):
                    await serving
                await workers
        finally:
            await engine.dispose()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m examples.invoice_host.serve")
    parser.add_argument("--pm-env", type=Path, required=True)
    parser.add_argument("--inv-env", type=Path, required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--accounting-config", type=Path, default=None)
    parser.add_argument("--provider-account-id", default=None)
    parser.add_argument("--table-prefix", default="")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url (or DATABASE_URL) is required")
    return args


async def _main(args: argparse.Namespace) -> int:
    environ = sandbox_environ(
        load_env_file(args.pm_env.expanduser()), load_env_file(args.inv_env.expanduser())
    )
    issuer_config = read_issuer_config(args.accounting_config or default_accounting_config())
    options = ServeOptions(
        database_url=args.database_url,
        issuer_config=issuer_config,
        artifact_root=args.artifact_root,
        pid_file=args.pid_file,
        port=args.port,
        provider_account_id=args.provider_account_id,
        table_prefix=args.table_prefix,
    )
    provider, http = sandbox_provider(environ)
    try:
        return await run(
            options,
            provider,
            environ,
            discover=lambda: discover_provider_account(
                provider, issuer_config, environ, options.credential_ref
            ),
        )
    finally:
        await http.aclose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except ServeError as exc:
        print(f"serve: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
