from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.conftest import unique_prefix

from invoice_module.adapters.sqlalchemy.migrations.schema_v1 import downgrade, upgrade
from invoice_module.adapters.sqlalchemy.tables import define_tables

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def _shape(engine: AsyncEngine, prefix: str) -> dict[str, object]:
    async with engine.connect() as conn:

        def inspect(sync: sa.Connection) -> dict[str, object]:
            inspector = sa.inspect(sync)
            names = sorted(name for name in inspector.get_table_names() if name.startswith(prefix))
            result: dict[str, object] = {}
            for name in names:
                short = name.removeprefix(prefix)
                result[short] = {
                    "columns": sorted(
                        (item["name"], str(item["type"]), item["nullable"])
                        for item in inspector.get_columns(name)
                    ),
                    "pk": tuple(inspector.get_pk_constraint(name)["constrained_columns"] or ()),
                    "uniques": sorted(
                        tuple(item["column_names"] or ())
                        for item in inspector.get_unique_constraints(name)
                    ),
                    "checks": sorted(
                        item["sqltext"] for item in inspector.get_check_constraints(name)
                    ),
                    "fks": sorted(
                        (
                            tuple(item["constrained_columns"]),
                            item["referred_table"].removeprefix(prefix),
                            tuple(item["referred_columns"]),
                        )
                        for item in inspector.get_foreign_keys(name)
                    ),
                    "indexes": sorted(
                        (
                            tuple(item["column_names"] or ()),
                            item["unique"],
                            str(item.get("dialect_options", {}).get("postgresql_where", "")),
                        )
                        for item in inspector.get_indexes(name)
                    ),
                }
            return result

        return await conn.run_sync(inspect)


def _upgrade(sync: sa.Connection, prefix: str) -> None:
    upgrade(Operations(MigrationContext.configure(sync)), prefix)


def _downgrade(sync: sa.Connection, prefix: str) -> None:
    downgrade(Operations(MigrationContext.configure(sync)), prefix)


async def test_define_tables_matches_schema_v1(engine: AsyncEngine) -> None:
    live_prefix, frozen_prefix = unique_prefix(), unique_prefix()
    metadata = sa.MetaData()
    define_tables(metadata, prefix=live_prefix)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.run_sync(_upgrade, frozen_prefix)
    live = await _shape(engine, live_prefix)
    frozen = await _shape(engine, frozen_prefix)
    assert live == frozen
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(_downgrade, frozen_prefix)


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
