"""The unique owner of the PostgreSQL fixtures shared by integration and sandbox tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables, define_tables

if TYPE_CHECKING:
    from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer | None]:
    """The session's ``postgres:17`` container; ``None`` when ``DATABASE_URL`` is set."""
    if os.environ.get("DATABASE_URL"):
        yield None
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17", driver=None) as container:
        yield container


@pytest.fixture(scope="session")
def pg_url(pg_container: PostgresContainer | None) -> str:
    if pg_container is None:
        return os.environ["DATABASE_URL"]
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return (
        f"postgresql+asyncpg://{pg_container.username}:{pg_container.password}"
        f"@{host}:{port}/{pg_container.dbname}"
    )


@pytest.fixture
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def unique_prefix() -> str:
    return f"t{uuid.uuid4().hex[:8]}_"


@pytest.fixture
async def inv_tables(engine: AsyncEngine) -> AsyncIterator[InvoiceTables]:
    metadata = sa.MetaData()
    tables = define_tables(metadata, prefix=unique_prefix())
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    try:
        yield tables
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
