"""``approvals`` commands: accounting lists, reads, approves and holds manual drafts.

Each command prints one JSON object per line on stdout. Exit codes: ``0`` when every document
was approved (or already approved) or held, ``3`` when a document could not be, ``2`` on a
usage error, ``1`` when the database could not be used. The database URL and its password
are never printed, and nothing is logged to a file.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from invoice_module.adapters.sqlalchemy.tables import define_tables
from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWorkFactory
from invoice_module.application.approve_draft_invoice import ApprovalNotAllowed
from invoice_module.application.hold_draft_invoice import HoldNotAllowed
from invoice_module.builder import InvoiceModule, build_invoice_module
from invoice_module.domain.errors import DocumentInFlight
from invoice_module.ports.clock import SystemClock

DATABASE_URL_ENV = "INVOICE_DATABASE_URL"
DEFAULT_TABLE_PREFIX = "inv_"
EXIT_OK, EXIT_DATABASE, EXIT_USAGE, EXIT_NOT_DONE = 0, 1, 2, 3
_PREFIX = re.compile(r"^[a-z_][a-z0-9_]*$")


class UsageError(Exception):
    pass


def add_parser(commands: Any) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--database-url",
        help=f"SQLAlchemy URL of the invoice database (default: ${DATABASE_URL_ENV})",
    )
    common.add_argument(
        "--table-prefix",
        default=DEFAULT_TABLE_PREFIX,
        help=f"prefix of the invoice tables (default: {DEFAULT_TABLE_PREFIX})",
    )
    group = commands.add_parser(
        "approvals", help="list, show, approve and hold manual-delivery drafts"
    )
    actions = group.add_subparsers(dest="action", required=True)

    listing = actions.add_parser(
        "list", parents=[common], help="drafts waiting for approval, one JSON line each"
    )
    listing.add_argument("--tenant-id")
    listing.add_argument(
        "--overdue-only", action="store_true", help="only drafts past the approval deadline"
    )
    listing.add_argument("--limit", type=int, default=100)
    listing.set_defaults(handler=_handler(_list))

    show = actions.add_parser(
        "show", parents=[common], help="the full draft (buyer, lines, tax) and its sha256"
    )
    show.add_argument("document_id", type=UUID)
    show.set_defaults(handler=_handler(_show))

    approve = actions.add_parser("approve", parents=[common], help="approve drafts as shown")
    approve.add_argument("document_ids", nargs="+", type=UUID, metavar="DOCUMENT_ID")
    approve.add_argument("--actor", required=True, help="who approves (recorded)")
    approve.add_argument("--note")
    approve.add_argument(
        "--expect-sha256",
        help="the snapshot_sha256 printed by show; refused if the draft changed since",
    )
    approve.set_defaults(handler=_handler(_approve))

    hold = actions.add_parser(
        "hold", parents=[common], help="hold a wrong draft before it is sent (opens a review)"
    )
    hold.add_argument("document_id", type=UUID)
    hold.add_argument("--actor", required=True, help="who holds (recorded)")
    hold.add_argument("--note", required=True, help="why the draft is wrong")
    hold.set_defaults(handler=_handler(_hold))


Command = Callable[[InvoiceModule, argparse.Namespace], Awaitable[int]]


def _handler(command: Command) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        try:
            _check_usage(args)
            url = _database_url(args)
        except UsageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        try:
            return asyncio.run(_run(command, args, url))
        except Exception as exc:
            # Driver errors (asyncpg raises its own on a refused login) may carry the URL or
            # the password in their text, so at this boundary only the type is printed.
            print(f"error: the database could not be used ({type(exc).__name__})", file=sys.stderr)
            return EXIT_DATABASE

    return run


def _check_usage(args: argparse.Namespace) -> None:
    if not _PREFIX.match(args.table_prefix):
        raise UsageError("--table-prefix must be lower-case letters, digits and underscores")
    for name in ("actor", "note"):
        value = getattr(args, name, None)
        if value is not None and not value.strip():
            raise UsageError(f"--{name} must not be blank")
    if getattr(args, "expect_sha256", None) is not None and len(args.document_ids) != 1:
        raise UsageError("--expect-sha256 applies to exactly one document")
    if getattr(args, "limit", 1) < 1:
        raise UsageError("--limit must be positive")


def _database_url(args: argparse.Namespace) -> str:
    url = args.database_url or os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise UsageError(f"pass --database-url or set {DATABASE_URL_ENV}")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    return url


async def _run(command: Command, args: argparse.Namespace, url: str) -> int:
    engine = create_async_engine(url, hide_parameters=True)
    try:
        tables = define_tables(sa.MetaData(), args.table_prefix)
        module = build_invoice_module(
            SqlAlchemyInvoiceUnitOfWorkFactory(
                async_sessionmaker(engine, expire_on_commit=False), tables
            ),
            SystemClock(),
        )
        return await command(module, args)
    finally:
        await engine.dispose()


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


async def _list(module: InvoiceModule, args: argparse.Namespace) -> int:
    pending = await module.list_pending_approvals.execute(
        tenant_id=args.tenant_id, overdue_only=args.overdue_only, limit=args.limit
    )
    for view in pending:
        _emit(
            {
                "document_id": view.document_id,
                "settlement_id": view.settlement_id,
                "host_ref": f"{view.host_ref_type}:{view.host_ref_id}",
                "total_vnd": view.total_vnd,
                "buyer_name": view.buyer_name,
                "settled_at": view.settled_at.isoformat(),
                "age_hours": round(view.age.total_seconds() / 3600, 2),
            }
        )
    return EXIT_OK


async def _show(module: InvoiceModule, args: argparse.Namespace) -> int:
    draft = await module.get_draft_for_approval.execute(args.document_id)
    if draft is None:
        _emit({"document_id": args.document_id, "result": "not_found"})
        return EXIT_NOT_DONE
    _emit(
        {
            "document_id": draft.document_id,
            "status": draft.status.value,
            "delivery_mode": draft.delivery_mode.value if draft.delivery_mode else None,
            "reference_code": draft.reference_code,
            "buyer": draft.buyer,
            "lines": list(draft.lines),
            "total_vnd": draft.total_vnd,
            "notes": draft.notes,
            "snapshot_sha256": draft.snapshot_sha256,
        }
    )
    return EXIT_OK


async def _approve(module: InvoiceModule, args: argparse.Namespace) -> int:
    code = EXIT_OK
    for document_id in args.document_ids:
        try:
            result = await module.approve_draft_invoice.execute(
                document_id,
                args.actor,
                args.note,
                expected_snapshot_sha256=args.expect_sha256,
            )
            outcome = "already_approved" if result.already_approved else "approved"
        except ApprovalNotAllowed as exc:
            outcome = f"not_allowed:{exc.reason}"
        except DocumentInFlight:
            outcome = "in_flight"
        except NoResultFound:
            outcome = "not_allowed:not_found"
        if outcome not in ("approved", "already_approved"):
            code = EXIT_NOT_DONE
        _emit({"document_id": document_id, "result": outcome})
    return code


async def _hold(module: InvoiceModule, args: argparse.Namespace) -> int:
    try:
        await module.hold_draft_invoice.execute(args.document_id, args.actor, args.note)
        outcome = "held"
    except HoldNotAllowed as exc:
        outcome = f"not_allowed:{exc.reason}"
    except DocumentInFlight:
        outcome = "in_flight"
    except NoResultFound:
        outcome = "not_allowed:not_found"
    _emit({"document_id": args.document_id, "result": outcome})
    return EXIT_OK if outcome == "held" else EXIT_NOT_DONE
