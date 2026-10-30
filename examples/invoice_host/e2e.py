"""Drive one order of the running example host (``serve``) from the outside.

    python -m examples.invoice_host.e2e place-order --database-url URL --amount N
    python -m examples.invoice_host.e2e pay-local --database-url URL --pm-env PM_ENV_FILE \\
        --url http://127.0.0.1:8787 --intent-id UUID
    python -m examples.invoice_host.e2e wait --database-url URL --intent-id UUID \\
        --expect-bank-reference REF --out FILE --timeout 1800

``place-order`` creates the order, its payment intent and its invoice draft in one
transaction and prints one JSON line with ``intent_id``, ``payment_reference``,
``amount_vnd`` and ``expires_at``: the transfer to make. The intent expires 30 minutes after
it is placed; a transfer that arrives later does not settle it, so make the SePay Test
transfer within that window (or place a new order). The draft line is the accountant's first
product line (name, unit, tax rate) with the unit price that makes the total, tax included,
exactly N.

``pay-local`` stands in for SePay Test: it signs a transfer into the intent's receiving
account (its bank code and number, as onboarded by ``serve``) with
``SEPAY_TEST_WEBHOOK_SECRET`` read from the payment env file in this process (never printed),
posts it to the host and prints the generated ``bank_reference`` (always ``LOCAL-...``). Any
answer but HTTP 200 exits 2.

``wait`` watches only the given intent until its settlement's original invoice is issued with
its PDF and XML, then writes a JSON summary. The settlement's bank transaction reference must
equal ``--expect-bank-reference`` (both trimmed and upper-cased, as payment-module stores
it): a ``LOCAL-`` reference is a locally signed transfer (``source=local_signed``), any other
is the reference read from the real SePay Test transaction (``source=sepay_test``). Exit 0
with the file written; 3 when the timeout passes first; 4 when the reference is missing or
different. Nothing is written unless the exit code is 0. A usage error exits 2 without
repeating a typed value that could hold the database URL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from payment_module.adapters.sepay.provider import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from invoice_module.cli.__main__ import _Parser
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.money import LineInput, compute_total
from tools.einvoice_probe.envfile import load_env_file

from .main import TENANT, HostTables, Onboarded, build, define_host_tables, place_order
from .serve import (
    PM_WEBHOOK_SECRET,
    default_accounting_config,
    parse_hiding_values,
    read_issuer_config,
)
from .sign_webhook import LOCAL_REFERENCE_PREFIX, signed_delivery

EXIT_TIMEOUT, EXIT_UNKNOWN_SOURCE, EXIT_USAGE = 3, 4, 2
# The sandbox refuses a goods line without an item code; the host supplies one.
DEFAULT_ITEM_CODE = "SANDBOX-PHOTOSHOP"


class UsageError(ValueError):
    pass


def normalise_reference(value: str | None) -> str | None:
    """Trimmed and upper-cased, as payment-module stores a webhook ``referenceCode``."""
    if value is None:
        return None
    text = value.strip().upper()
    return text or None


def order_lines(
    product: Mapping[str, Any], amount_vnd: int, item_code: str
) -> tuple[Mapping[str, Any], ...]:
    """One line of ``product`` (quantity 1) whose total with tax is exactly ``amount_vnd``."""
    if amount_vnd <= 0:
        raise UsageError("--amount must be a positive number of VND")
    rate = product["tax_rate"]
    estimate = int(Decimal(amount_vnd * 100) / Decimal(100 + max(rate, 0)))
    for price in range(max(estimate - 2, 1), estimate + 3):
        line = {
            "name": product["name"],
            "unit": product["unit"],
            "quantity": Decimal("1"),
            "unit_price_vnd": price,
            "tax_rate": rate,
            "item_code": item_code,
        }
        if compute_total([LineInput(**line)]) == amount_vnd:
            return (line,)
    raise UsageError(f"no whole unit price gives a total of {amount_vnd} VND at {rate}% tax")


async def _onboarded(engine: AsyncEngine, tables: HostTables) -> Onboarded:
    merchants = tables.payment.merchants
    accounts = tables.payment.receiving_accounts
    async with engine.connect() as db:
        row = (
            await db.execute(
                sa.select(merchants.c.id, accounts.c.id.label("account_id"))
                .join(accounts, accounts.c.merchant_id == merchants.c.id)
                .where(merchants.c.tenant_id == TENANT)
            )
        ).first()
    if row is None:
        raise UsageError("the host is not onboarded; start serve first")
    return Onboarded(row.id, row.account_id, None, None)


async def place(
    database_url: str,
    amount_vnd: int,
    *,
    issuer_config: Mapping[str, Any],
    item_code: str = DEFAULT_ITEM_CODE,
    table_prefix: str = "",
) -> dict[str, Any]:
    lines = order_lines(issuer_config["product_lines"][0], amount_vnd, item_code)
    tables = define_host_tables(table_prefix)
    engine = create_async_engine(database_url)
    try:
        host = build(engine, None, Path("."), tables=tables, environ={})  # type: ignore[arg-type]
        onboarded = await _onboarded(engine, tables)
        order_id = f"e2e-{uuid4()}"
        created = await place_order(
            host, onboarded, order_id, lines, delivery_mode=DeliveryMode.AUTO
        )
    finally:
        await engine.dispose()
    intent = created.intent
    return {
        "intent_id": str(intent.id),
        "payment_reference": intent.payment_reference,
        "amount_vnd": intent.amount.value,
        "expires_at": intent.expires_at.isoformat(),
        "order_id": order_id,
    }


def signed_transfer(
    secret: str,
    bank_code: str,
    account_number: str,
    amount_vnd: int,
    code: str,
    *,
    reference_code_prefix: str = LOCAL_REFERENCE_PREFIX,
) -> tuple[bytes, dict[str, str], str]:
    """``signed_delivery`` into ``bank_code``'s account ``account_number``: that helper always
    names Vietcombank as the gateway, and the gateway is part of the account a webhook
    settles into, so the body names ``bank_code`` and is signed again."""
    raw, headers, bank_reference = signed_delivery(
        secret, account_number, amount_vnd, code, code, reference_code_prefix=reference_code_prefix
    )
    body = json.loads(raw) | {"gateway": bank_code}
    raw = json.dumps(body).encode()
    headers = {**headers, SIGNATURE_HEADER: sign(raw, secret, int(headers[TIMESTAMP_HEADER]))}
    return raw, headers, bank_reference


async def pay_local(
    database_url: str,
    secret: str,
    base_url: str,
    intent_id: UUID,
    *,
    table_prefix: str = "",
) -> str:
    """Post a locally signed transfer paying ``intent_id``; returns its bank reference."""
    tables = define_host_tables(table_prefix)
    intents = tables.payment.payment_intents
    accounts = tables.payment.receiving_accounts
    connections = tables.payment.provider_connections
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as db:
            row = (
                await db.execute(
                    sa.select(
                        intents.c.amount_vnd,
                        intents.c.payment_reference,
                        accounts.c.bank_code,
                        accounts.c.account_number,
                        connections.c.locator,
                    )
                    .join(accounts, accounts.c.id == intents.c.receiving_account_id)
                    .join(connections, connections.c.merchant_id == intents.c.merchant_id)
                    .where(intents.c.id == intent_id)
                )
            ).first()
    finally:
        await engine.dispose()
    if row is None:
        raise UsageError(f"no intent {intent_id}")
    raw, headers, bank_reference = signed_transfer(
        secret, row.bank_code, row.account_number, row.amount_vnd, row.payment_reference
    )
    url = f"{base_url.rstrip('/')}/webhooks/sepay/{row.locator}"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, content=raw, headers=headers)
    if response.status_code != 200:
        raise UsageError(f"the host answered HTTP {response.status_code}")
    return bank_reference


async def _issued(
    engine: AsyncEngine, tables: HostTables, intent_id: UUID
) -> dict[str, Any] | None:
    """The intent's issued original invoice with its provenance, or ``None`` while waiting."""
    settlements = tables.payment.settlements
    transactions = tables.payment.provider_transactions
    documents = tables.invoice.documents
    receipts = tables.invoice.settlement_receipts
    reviews = tables.invoice.review_cases
    async with engine.connect() as db:
        settlement = (
            await db.execute(sa.select(settlements).where(settlements.c.intent_id == intent_id))
        ).first()
        if settlement is None:
            return None
        originals = (
            await db.execute(
                sa.select(documents).where(
                    documents.c.settlement_id == settlement.id, documents.c.kind == "original"
                )
            )
        ).all()
        if len(originals) != 1:
            return None
        doc = originals[0]
        if doc.status != "issued" or not (doc.pdf_uri and doc.xml_uri):
            return None
        receipt = (
            await db.execute(
                sa.select(receipts.c.transaction_id, receipts.c.tenant_id, receipts.c.environment)
                .where(receipts.c.settlement_id == settlement.id)
                .limit(1)
            )
        ).first()
        bank_reference = None
        if receipt is not None:
            bank_reference = await db.scalar(
                sa.select(transactions.c.bank_reference).where(
                    transactions.c.id == receipt.transaction_id,
                    transactions.c.tenant_id == receipt.tenant_id,
                    transactions.c.environment == receipt.environment,
                )
            )
        cases = await db.scalar(
            sa.select(sa.func.count()).select_from(reviews).where(reviews.c.document_id == doc.id)
        )
    return {
        "intent_id": str(intent_id),
        "settlement_id": str(settlement.id),
        "bank_reference": bank_reference,
        "status": doc.status,
        "original_documents": len(originals),
        "total_equals_settled": doc.total_vnd == doc.settled_amount_vnd == settlement.amount_vnd,
        "invoice_number": doc.invoice_number,
        "tax_authority_code": doc.tax_authority_code,
        "pdf_sha256": doc.pdf_sha256,
        "xml_sha256": doc.xml_sha256,
        # Operator actions on the way to the invoice: review cases and an approval.
        "manual_invoice_steps": int(cases) + (doc.approved_by is not None),
    }


def source_of(bank_reference: str | None, expected: str) -> str:
    stored, wanted = normalise_reference(bank_reference), normalise_reference(expected)
    if stored is None or stored != wanted:
        return "unknown"
    return "local_signed" if stored.startswith(LOCAL_REFERENCE_PREFIX) else "sepay_test"


async def wait(
    database_url: str,
    intent_id: UUID,
    expected_reference: str,
    out: Path,
    timeout_s: float,
    *,
    interval_s: float = 1.0,
    table_prefix: str = "",
) -> int:
    tables = define_host_tables(table_prefix)
    engine = create_async_engine(database_url)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            found = await _issued(engine, tables, intent_id)
            if found is not None:
                break
            if time.monotonic() >= deadline:
                print(
                    f"e2e wait: intent {intent_id} not issued in {timeout_s:g} s", file=sys.stderr
                )
                return EXIT_TIMEOUT
            await asyncio.sleep(min(interval_s, max(deadline - time.monotonic(), 0)))
    finally:
        await engine.dispose()
    found["source"] = source_of(found["bank_reference"], expected_reference)
    if found["source"] == "unknown":
        print("e2e wait: the settlement's bank reference is not the expected one", file=sys.stderr)
        return EXIT_UNKNOWN_SOURCE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"e2e wait: wrote {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m examples.invoice_host.e2e")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.add_argument("--database-url", required=True)
        sub.add_argument("--table-prefix", default="")
        return sub

    order = command("place-order")
    order.add_argument("--amount", type=int, required=True)
    order.add_argument("--accounting-config", type=Path, default=None)
    order.add_argument("--item-code", default=DEFAULT_ITEM_CODE)
    paying = command("pay-local")
    paying.add_argument("--pm-env", type=Path, required=True)
    paying.add_argument("--url", required=True)
    paying.add_argument("--intent-id", type=UUID, required=True)
    waiting = command("wait")
    waiting.add_argument("--intent-id", type=UUID, required=True)
    waiting.add_argument("--expect-bank-reference", required=True)
    waiting.add_argument("--out", type=Path, required=True)
    waiting.add_argument("--timeout", type=float, required=True)
    waiting.add_argument("--interval", type=float, default=1.0)
    return parser


async def run(argv: Sequence[str]) -> int:
    """One command; prints its JSON line (``place-order``, ``pay-local``) and returns the
    exit code."""
    args = parse_hiding_values(build_parser(), list(argv))
    try:
        if args.command == "place-order":
            config = read_issuer_config(args.accounting_config or default_accounting_config())
            placed = await place(
                args.database_url,
                args.amount,
                issuer_config=config,
                item_code=args.item_code,
                table_prefix=args.table_prefix,
            )
            print(json.dumps(placed), flush=True)
            return 0
        if args.command == "pay-local":
            secret = load_env_file(args.pm_env.expanduser()).get(PM_WEBHOOK_SECRET)
            if not secret:
                raise UsageError(f"the payment env file has no {PM_WEBHOOK_SECRET}")
            reference = await pay_local(
                args.database_url, secret, args.url, args.intent_id, table_prefix=args.table_prefix
            )
            print(json.dumps({"bank_reference": reference}), flush=True)
            return 0
        return await wait(
            args.database_url,
            args.intent_id,
            args.expect_bank_reference,
            args.out,
            args.timeout,
            interval_s=args.interval,
            table_prefix=args.table_prefix,
        )
    except UsageError as exc:
        print(f"e2e {args.command}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
