import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.onboarding.world import OnboardingWorld

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables


@pytest.fixture
async def world(engine: AsyncEngine, inv_tables: InvoiceTables) -> OnboardingWorld:
    return OnboardingWorld(engine, inv_tables)
