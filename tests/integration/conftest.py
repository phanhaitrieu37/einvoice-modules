from pathlib import Path

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    unmarked = [
        item.nodeid
        for item in items
        if here in item.path.parents and item.get_closest_marker("integration") is None
    ]
    if unmarked:
        raise pytest.UsageError(f"integration tests without the integration marker: {unmarked}")
