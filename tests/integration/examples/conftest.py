import secrets

import pytest
from examples.invoice_host.main import define_host_tables
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import unique_prefix


@pytest.fixture
def environ():
    """Synthetic secrets the example resolves through ``env:NAME`` references."""
    return {
        "SEPAY_WEBHOOK_SECRET": secrets.token_hex(32),
        "SEPAY_EINVOICE_CLIENT_ID": "synthetic-client",
        "SEPAY_EINVOICE_CLIENT_SECRET": "synthetic-secret",
    }


@pytest.fixture
async def host_tables(pg_url):
    """The example's tables under a unique prefix, dropped afterwards."""
    tables = define_host_tables(unique_prefix())
    yield tables
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(tables.metadata.drop_all)
    finally:
        await engine.dispose()
