"""The unique owner of the PostgreSQL fixtures shared by integration and sandbox tests."""

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def pg_container() -> Iterator[object | None]:
    """The session's ``postgres:17`` container; ``None`` when ``DATABASE_URL`` is set."""
    if os.environ.get("DATABASE_URL"):
        yield None
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17", driver=None) as container:
        yield container


@pytest.fixture(scope="session")
def pg_url(pg_container: object | None) -> str:
    if pg_container is None:
        return os.environ["DATABASE_URL"]
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql+asyncpg://{pg_container.username}:{pg_container.password}@{host}:{port}/{pg_container.dbname}"
