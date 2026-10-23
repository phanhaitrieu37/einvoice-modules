import pytest
from tests.integration.worker.world import TransitionRecorder, World


@pytest.fixture
async def world(engine, inv_tables, tmp_path):
    world = World(engine, inv_tables, tmp_path / "artifacts")
    await world.seed_account()
    return world


@pytest.fixture
def transitions(monkeypatch):
    return TransitionRecorder(monkeypatch)
