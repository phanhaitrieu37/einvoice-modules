from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import unique_prefix

from invoice_module.adapters.sqlalchemy.migrations import schema_v1
from invoice_module.adapters.sqlalchemy.tables import TABLE_COUNT, define_tables

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _strip(value: Any, prefix: str) -> Any:
    if isinstance(value, str):
        return value.replace(prefix, "")
    if isinstance(value, tuple):
        return tuple(_strip(item, prefix) for item in value)
    if isinstance(value, list):
        return [_strip(item, prefix) for item in value]
    if isinstance(value, dict):
        return {_strip(key, prefix): _strip(item, prefix) for key, item in value.items()}
    return value


def _snapshot(sync_conn: sa.Connection, prefix: str) -> dict[str, Any]:
    inspector = sa.inspect(sync_conn)
    snapshot: dict[str, Any] = {}
    for table in sorted(name for name in inspector.get_table_names() if name.startswith(prefix)):
        columns = [
            {
                "name": column["name"],
                "type": column["type"].compile(dialect=sync_conn.dialect),
                "nullable": column["nullable"],
                "default": column["default"],
            }
            for column in inspector.get_columns(table)
        ]
        primary_key = inspector.get_pk_constraint(table)
        uniques = sorted(
            (item["name"], tuple(item["column_names"]))
            for item in inspector.get_unique_constraints(table)
        )
        checks = sorted(
            (item["name"], item["sqltext"]) for item in inspector.get_check_constraints(table)
        )
        foreign_keys = sorted(
            (
                item["name"],
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
                tuple(sorted(item["options"].items())),
            )
            for item in inspector.get_foreign_keys(table)
        )
        indexes = sorted(
            (
                item["name"],
                tuple(item["column_names"]),
                bool(item["unique"]),
                item.get("dialect_options", {}).get("postgresql_where"),
            )
            for item in inspector.get_indexes(table)
        )
        snapshot[table] = {
            "columns": columns,
            "primary_key": (primary_key["name"], tuple(primary_key["constrained_columns"])),
            "uniques": uniques,
            "checks": checks,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }
    return _strip(snapshot, prefix)


def _upgrade(sync_conn: sa.Connection, prefix: str) -> None:
    schema_v1.upgrade(Operations(MigrationContext.configure(sync_conn)), prefix=prefix)


def _downgrade(sync_conn: sa.Connection, prefix: str) -> None:
    schema_v1.downgrade(Operations(MigrationContext.configure(sync_conn)), prefix=prefix)


@pytest.fixture
async def both(engine: AsyncEngine) -> AsyncIterator[tuple[dict[str, Any], dict[str, Any]]]:
    live_prefix, frozen_prefix = unique_prefix(), unique_prefix()
    metadata = sa.MetaData()
    define_tables(metadata, prefix=live_prefix)
    frozen_created = False
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.run_sync(_upgrade, frozen_prefix)
        frozen_created = True
        async with engine.connect() as conn:
            live = await conn.run_sync(_snapshot, live_prefix)
            frozen = await conn.run_sync(_snapshot, frozen_prefix)
        yield live, frozen
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            if frozen_created:
                await conn.run_sync(_downgrade, frozen_prefix)


async def test_define_tables_matches_schema_v1(both: tuple[dict[str, Any], dict[str, Any]]) -> None:
    live, frozen = both
    assert len(live) == len(frozen) == TABLE_COUNT
    assert live.keys() == frozen.keys()
    for table in live:
        for part in ("columns", "primary_key", "uniques", "checks", "foreign_keys", "indexes"):
            assert live[table][part] == frozen[table][part], f"{table}.{part} differs"


async def test_snapshot_covers_domain_constraints(
    both: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    live, _ = both
    docs = live["documents"]
    receipt = live["settlement_receipts"]
    parent = next(fk for fk in docs["foreign_keys"] if fk[0] == "documents_parent_fk")
    receipt_fk = next(
        fk for fk in receipt["foreign_keys"] if fk[0] == "settlement_receipts_document_fk"
    )
    assert len(parent[1]) == 6
    assert parent[4] == ()
    assert receipt_fk[0] == "settlement_receipts_document_fk"
    assert len(receipt_fk[1]) == 6
    doc_indexes = {index[0]: index[3] for index in docs["indexes"]}
    review_indexes = {index[0]: index[3] for index in live["review_cases"]["indexes"]}
    assert "kind" in doc_indexes["documents_original_settlement_uq"]
    assert "original" in doc_indexes["documents_original_settlement_uq"]
    assert "status" in review_indexes["review_cases_one_open_uq"]
    assert "open" in review_indexes["review_cases_one_open_uq"]


async def test_downgrade_drops_every_table(engine: AsyncEngine) -> None:
    prefix = unique_prefix()
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade, prefix)
        await conn.run_sync(_downgrade, prefix)
    async with engine.connect() as conn:
        remaining = await conn.run_sync(
            lambda sync: [
                name for name in sa.inspect(sync).get_table_names() if name.startswith(prefix)
            ]
        )
    assert remaining == []
