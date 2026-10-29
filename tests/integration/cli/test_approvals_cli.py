"""The ``approvals`` command line, run in process through ``main(argv)`` on real PostgreSQL.

``main`` runs its own event loop, so these tests are synchronous and seed the database in
separate event loops through the worker test world.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import unique_prefix
from tests.fakes.fake_einvoice_provider import TRANSIENT
from tests.integration.worker.world import World

from invoice_module.adapters.sqlalchemy.repositories.documents import snapshot_sha256
from invoice_module.adapters.sqlalchemy.tables import define_tables
from invoice_module.application.queries import ListPendingApprovals
from invoice_module.cli import approvals
from invoice_module.cli.__main__ import build_parser, main
from invoice_module.domain.enums import DeliveryMode

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

MANUAL = DeliveryMode.MANUAL
ACTOR = "accountant-synthetic"
LIST_KEYS = {
    "document_id", "settlement_id", "host_ref", "total_vnd", "buyer_name", "settled_at",
    "age_hours",
}  # fmt: skip


class CliDb:
    """One table set; every ``run`` opens its own engine and world (fresh fake clock)."""

    def __init__(self, url: str, root: Path) -> None:
        self.url, self.root = url, root
        self.prefix = unique_prefix()
        self.merchant_id, self.account_id = uuid4(), uuid4()

    def run(self, scenario: Callable[[World], Awaitable[Any]]) -> Any:
        return asyncio.run(self._run(scenario))

    async def _run(self, scenario: Callable[[World], Awaitable[Any]]) -> Any:
        engine = create_async_engine(self.url)
        try:
            world = World(engine, define_tables(sa.MetaData(), self.prefix), self.root)
            world.merchant_id, world.account_id = self.merchant_id, self.account_id
            world.module = world.build()
            return await scenario(world)
        finally:
            await engine.dispose()

    def cli(self, *args: str) -> int:
        """Run ``approvals ARGS`` against this table set."""
        action, *rest = args
        return main(
            ["approvals", action, "--database-url", self.url, "--table-prefix", self.prefix, *rest]
        )

    def doc(self, document_id) -> Any:
        async def read(world: World) -> Any:
            return await world.doc(document_id)

        return self.run(read)

    def awaiting(self, **settle: Any):
        async def seed(world: World):
            settled = await world.settle(mode=MANUAL, **settle)
            await world.issue()
            assert (await world.doc(settled.document_id)).status == "awaiting_approval"
            return settled

        return self.run(seed)

    def approved(self):
        settled = self.awaiting()

        async def approve(world: World) -> None:
            await world.approve(settled)

        self.run(approve)
        assert self.doc(settled.document_id).status == "approved"
        return settled


@pytest.fixture
def db(pg_url, tmp_path):
    db = CliDb(pg_url, tmp_path / "artifacts")
    metadata = sa.MetaData()
    define_tables(metadata, db.prefix)

    async def create(world: World) -> None:
        async with world.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        await world.seed_account()

    async def drop(world: World) -> None:
        async with world.engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)

    db.run(create)
    yield db
    db.run(drop)


def lines(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def test_list_prints_pending_approvals_as_json_lines(db, capsys):
    old = db.awaiting()
    other = db.awaiting(amount=250)

    async def recent_and_auto(world: World):
        world.clock.set(datetime.now(UTC))
        recent = await world.settle(mode=MANUAL)
        auto = await world.settle()
        await world.issue()
        return recent, auto

    recent, auto = db.run(recent_and_auto)
    capsys.readouterr()
    assert db.cli("list") == 0
    listed = {row["document_id"]: row for row in lines(capsys.readouterr().out)}
    assert set(listed) == {str(s.document_id) for s in (old, other, recent)}
    assert all(set(row) == LIST_KEYS for row in listed.values())
    first = listed[str(old.document_id)]
    assert (first["settlement_id"], first["host_ref"], first["total_vnd"]) == (
        str(old.settlement_id),
        f"order:{old.host_ref_id}",
        100,
    )
    assert first["buyer_name"] == "Synthetic fallback buyer"
    assert listed[str(other.document_id)]["total_vnd"] == 250
    assert datetime.fromisoformat(first["settled_at"]) == db.doc(old.document_id).settled_at
    assert first["age_hours"] > 24 > listed[str(recent.document_id)]["age_hours"] >= 0
    hours = (datetime.now(UTC) - db.doc(old.document_id).settled_at).total_seconds() / 3600
    assert hours - 0.1 < first["age_hours"] <= hours + 0.01  # printed to two decimals
    # Settled on Wednesday 21 October: more than one business day ago, unlike the recent one.
    assert db.cli("list", "--overdue-only") == 0
    overdue = {row["document_id"] for row in lines(capsys.readouterr().out)}
    assert overdue == {str(old.document_id), str(other.document_id)}
    assert db.cli("list", "--tenant-id", "other-tenant") == 0
    assert capsys.readouterr().out == ""
    assert db.cli("list", "--limit", "1") == 0
    assert len(lines(capsys.readouterr().out)) == 1
    assert db.doc(auto.document_id).status != "awaiting_approval"


def test_approve_records_actor_and_exits_zero(db, capsys):
    first, second = db.awaiting(), db.awaiting()
    capsys.readouterr()
    before = datetime.now(UTC)
    code = db.cli("approve", str(first.document_id), str(second.document_id), "--actor", ACTOR)
    assert code == 0
    assert lines(capsys.readouterr().out) == [
        {"document_id": str(first.document_id), "result": "approved"},
        {"document_id": str(second.document_id), "result": "approved"},
    ]
    for settled in (first, second):
        doc = db.doc(settled.document_id)
        assert (doc.status, doc.approved_by) == ("approved", ACTOR)
        assert doc.approved_at >= before
    assert db.cli("approve", str(first.document_id), "--actor", "someone-else") == 0
    assert lines(capsys.readouterr().out) == [
        {"document_id": str(first.document_id), "result": "already_approved"}
    ]
    assert db.doc(first.document_id).approved_by == ACTOR


def test_approve_requires_actor(db, capsys):
    settled = db.awaiting()
    assert db.cli("approve", str(settled.document_id)) == 2
    assert db.cli("approve", str(settled.document_id), "--actor", "   ") == 2
    assert "--actor" in capsys.readouterr().err
    doc = db.doc(settled.document_id)
    assert (doc.status, doc.approved_by) == ("awaiting_approval", None)


def test_approve_not_allowed_exits_three(db, capsys):
    awaiting, busy = db.awaiting(), db.awaiting()

    async def auto_and_lease(world: World):
        auto = await world.settle()
        await world.issue()
        await world.set_columns(
            busy.document_id, lease_until=datetime.now(UTC) + timedelta(minutes=5)
        )
        return auto

    auto = db.run(auto_and_lease)
    status = db.doc(auto.document_id).status
    unknown = uuid4()
    capsys.readouterr()
    ids = [awaiting.document_id, auto.document_id, busy.document_id, unknown]
    assert db.cli("approve", *map(str, ids), "--actor", ACTOR) == 3
    assert [row["result"] for row in lines(capsys.readouterr().out)] == [
        "approved",
        f"not_allowed:{status}",
        "in_flight",
        "not_allowed:not_found",
    ]
    assert db.doc(awaiting.document_id).status == "approved"
    assert db.doc(busy.document_id).status == "awaiting_approval"


def test_cli_never_prints_database_url(db, capsys, monkeypatch):
    settled = db.awaiting()
    url = make_url(db.url)
    secrets = {db.url, url.password, url.render_as_string(hide_password=False)}
    capsys.readouterr()
    assert db.cli("list") == 0
    assert db.cli("show", str(settled.document_id)) == 0
    assert db.cli("approve", str(settled.document_id), "--actor", ACTOR) == 0
    # The same commands through the environment variable.
    monkeypatch.setenv("INVOICE_DATABASE_URL", db.url)
    assert main(["approvals", "list", "--table-prefix", db.prefix]) == 0
    # A refused login prints the error type only.
    wrong = url.set(password="synthetic-wrong-password")
    wrong_url = wrong.render_as_string(hide_password=False)
    secrets |= {wrong_url, "synthetic-wrong-password"}
    assert main(["approvals", "list", "--database-url", wrong_url]) == 1
    monkeypatch.delenv("INVOICE_DATABASE_URL")
    assert main(["approvals", "list"]) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "the database could not be used" in captured.err
    assert all(secret not in output for secret in secrets)


def test_hold_requires_note_and_records_actor(db, capsys):
    settled = db.awaiting()
    document = str(settled.document_id)
    assert db.cli("hold", document, "--actor", ACTOR) == 2
    assert db.cli("hold", document, "--actor", ACTOR, "--note", "  ") == 2
    assert db.cli("hold", document, "--note", "Wrong buyer") == 2
    assert db.doc(settled.document_id).status == "awaiting_approval"
    capsys.readouterr()
    assert db.cli("hold", document, "--actor", ACTOR, "--note", "Wrong buyer") == 0
    assert lines(capsys.readouterr().out) == [{"document_id": document, "result": "held"}]
    assert db.doc(settled.document_id).status == "blocked"

    async def held(world: World):
        return await world.reason_details(settled.document_id, "DRAFT_HELD")

    assert db.run(held) == {"note": "Wrong buyer", "held_by": ACTOR}
    assert db.cli("hold", document, "--actor", ACTOR, "--note", "Again") == 3
    assert lines(capsys.readouterr().out) == [
        {"document_id": document, "result": "not_allowed:blocked"}
    ]


def test_hold_approved_before_send_needs_no_credentials(db, capsys, monkeypatch):
    settled = db.approved()
    doc = db.doc(settled.document_id)
    assert (doc.status, doc.send_started_at) == ("approved", None)
    # Nothing exists at the provider yet, so no credential is resolved for a hold.
    monkeypatch.delenv("SYNTHETIC_CREDENTIAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("SYNTHETIC_CREDENTIAL_CLIENT_SECRET", raising=False)
    capsys.readouterr()
    code = db.cli("hold", str(settled.document_id), "--actor", ACTOR, "--note", "Wrong item")
    assert code == 0
    assert lines(capsys.readouterr().out) == [
        {"document_id": str(settled.document_id), "result": "held"}
    ]
    doc = db.doc(settled.document_id)
    assert (doc.status, doc.approved_by, doc.approved_at) == ("blocked", None, None)


def test_hold_after_send_started_not_allowed_exits_three(db, capsys):
    settled = db.approved()

    async def send_once_then_reset_attempts(world: World) -> None:
        # One POST goes out and is lost; an operator retry later resets the attempt counter.
        world.provider.script_create(settled.reference, TRANSIENT)
        await world.issue()
        await world.set_columns(settled.document_id, attempts=0)

    db.run(send_once_then_reset_attempts)
    doc = db.doc(settled.document_id)
    assert (doc.status, doc.attempts) == ("approved", 0)
    assert doc.send_started_at is not None
    capsys.readouterr()
    code = db.cli("hold", str(settled.document_id), "--actor", ACTOR, "--note", "Too late")
    assert code == 3
    assert lines(capsys.readouterr().out) == [
        {"document_id": str(settled.document_id), "result": "not_allowed:send_started"}
    ]
    assert db.doc(settled.document_id).status == "approved"


def test_show_prints_full_snapshot_and_hash(db, capsys):
    settled = db.awaiting(amount=110)
    capsys.readouterr()
    assert db.cli("show", str(settled.document_id)) == 0
    [shown] = lines(capsys.readouterr().out)
    doc = db.doc(settled.document_id)
    assert shown["snapshot_sha256"] == snapshot_sha256(doc.document)
    assert (shown["status"], shown["delivery_mode"], shown["reference_code"]) == (
        "awaiting_approval",
        "manual",
        settled.reference,
    )
    assert shown["buyer"] == {"name": "Synthetic fallback buyer", "address": ""}
    assert shown["lines"] == doc.document["lines"]
    [line] = shown["lines"]
    assert (line["name"], line["item_code"], line["tax_rate"]) == (
        "Synthetic service",
        "SYN-ITEM-1",
        0,
    )
    assert (line["tax_vnd"], line["line_total_vnd"], shown["total_vnd"]) == (0, 110, 110)
    unknown = uuid4()
    assert db.cli("show", str(unknown)) == 3
    assert lines(capsys.readouterr().out) == [{"document_id": str(unknown), "result": "not_found"}]


def test_approve_with_expect_sha256_mismatch_not_allowed(db, capsys):
    settled, other = db.awaiting(), db.awaiting()
    document = str(settled.document_id)
    capsys.readouterr()
    assert db.cli("approve", document, "--actor", ACTOR, "--expect-sha256", "0" * 64) == 3
    assert lines(capsys.readouterr().out) == [
        {"document_id": document, "result": "not_allowed:snapshot_changed"}
    ]
    assert db.doc(settled.document_id).status == "awaiting_approval"
    assert db.cli("show", document) == 0
    digest = lines(capsys.readouterr().out)[0]["snapshot_sha256"]
    two = (document, str(other.document_id))
    assert db.cli("approve", *two, "--actor", ACTOR, "--expect-sha256", digest) == 2
    assert db.cli("approve", document, "--actor", ACTOR, "--expect-sha256", digest) == 0
    assert lines(capsys.readouterr().out) == [{"document_id": document, "result": "approved"}]
    doc = db.doc(settled.document_id)
    assert (doc.status, doc.approved_snapshot_sha256) == ("approved", digest)


def test_hold_in_flight_exits_three(db, capsys):
    settled = db.awaiting()

    async def lease(world: World) -> None:
        await world.set_columns(
            settled.document_id, lease_until=datetime.now(UTC) + timedelta(minutes=5)
        )

    db.run(lease)
    capsys.readouterr()
    code = db.cli("hold", str(settled.document_id), "--actor", ACTOR, "--note", "Wrong buyer")
    assert code == 3
    assert lines(capsys.readouterr().out) == [
        {"document_id": str(settled.document_id), "result": "in_flight"}
    ]
    assert db.doc(settled.document_id).status == "awaiting_approval"


def test_approve_in_flight_alone_exits_three(db, capsys):
    settled = db.awaiting()

    async def lease(world: World) -> None:
        await world.set_columns(
            settled.document_id, lease_until=datetime.now(UTC) + timedelta(minutes=5)
        )

    db.run(lease)
    capsys.readouterr()
    assert db.cli("approve", str(settled.document_id), "--actor", ACTOR) == 3
    assert lines(capsys.readouterr().out) == [
        {"document_id": str(settled.document_id), "result": "in_flight"}
    ]
    doc = db.doc(settled.document_id)
    assert (doc.status, doc.approved_by) == ("awaiting_approval", None)


SYNTHETIC_PASSWORD = "SYNTH-PW-MARKER-7f3a"
LEAKY_URL = f"postgresql://synuser:{SYNTHETIC_PASSWORD}@127.0.0.1:1/synthetic"


@pytest.mark.parametrize(
    ("argv", "code", "shown"),
    [
        (["approvals", "list", "--database_url", LEAKY_URL], 2, "--database_url"),
        (["approvals", "list", f"--database_url={LEAKY_URL}"], 2, "--database_url"),
        (["approvals", "list", LEAKY_URL], 2, "unrecognized arguments"),
        (["approvals", "show", LEAKY_URL, "--database-url", LEAKY_URL], 2, "document_id"),
        (["approvals", "lst", "--database-url", LEAKY_URL], 2, "lst"),
        (["approvals", "list", "--limit", LEAKY_URL], 2, "--limit"),
        (["approvals", "list", "--database-url"], 2, "--database-url"),
        (["approvals", "list", "--limit", LEAKY_URL.replace("synuser:", "synuser:\\")], 2,
         "--limit"),
        (["approvals", "list", f"synuser:{SYNTHETIC_PASSWORD}@127.0.0.1"], 2,
         "unrecognized arguments"),
        (["approvals", "list", "--database-url", LEAKY_URL.replace("postgresql", "nosuchdb")],
         1, "the database could not be used"),
        (["approvals", "list", "--database-url", LEAKY_URL], 1, "the database could not be used"),
    ],
    ids=[
        "misspelt-option", "misspelt-option-equals", "stray-positional", "url-as-document-id",
        "misspelt-action", "url-as-limit", "missing-value", "escaped-url-as-limit",
        "userinfo-without-scheme", "invalid-scheme", "unreachable",
    ],
)  # fmt: skip
def test_cli_never_prints_the_password_on_usage_or_connection_errors(capsys, argv, code, shown):
    assert main(argv) == code
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert SYNTHETIC_PASSWORD not in output
    assert "synuser" not in output
    assert shown in captured.err


def test_table_prefix_defaults_to_inv():
    args = build_parser().parse_args(["approvals", "list"])
    assert args.table_prefix == "inv_"


def test_database_url_flag_wins_over_environment(db, capsys, monkeypatch):
    settled = db.awaiting()
    wrong = make_url(db.url).set(password="synthetic-wrong-password")
    monkeypatch.setenv("INVOICE_DATABASE_URL", wrong.render_as_string(hide_password=False))
    capsys.readouterr()
    assert db.cli("list") == 0
    [row] = lines(capsys.readouterr().out)
    assert row["document_id"] == str(settled.document_id)
    # Without the flag the environment is used.
    assert main(["approvals", "list", "--table-prefix", db.prefix]) == 1
    assert "the database could not be used" in capsys.readouterr().err


def test_a_failure_outside_the_database_is_not_reported_as_one(db, capsys, monkeypatch):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("synthetic failure")

    monkeypatch.setattr(ListPendingApprovals, "execute", broken)
    capsys.readouterr()
    assert db.cli("list") == 1
    err = capsys.readouterr().err
    assert "error: the command failed (ValueError)" in err
    assert "database" not in err
    assert "synthetic failure" not in err


def test_a_database_error_carrying_the_url_prints_only_its_type(capsys, monkeypatch):
    def refuse(url: str, **kwargs: Any) -> Any:
        raise sa.exc.ArgumentError(f"could not use {url}")

    monkeypatch.setattr(approvals, "create_async_engine", refuse)
    assert main(["approvals", "list", "--database-url", LEAKY_URL]) == 1
    captured = capsys.readouterr()
    assert captured.err == "error: the database could not be used (ArgumentError)\n"
    assert SYNTHETIC_PASSWORD not in captured.out + captured.err
