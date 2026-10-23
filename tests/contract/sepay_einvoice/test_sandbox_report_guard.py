"""The sandbox suite renders a failure without frame arguments or locals.

Runs offline: the hook from ``tests/sandbox/conftest.py`` gets a synthetic failure whose
frames carry a synthetic bearer header.
"""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.contract

BEARER = "Bearer synthetic-sandbox-access-token"


def _sandbox_conftest() -> ModuleType:
    path = Path(__file__).parents[2] / "sandbox" / "conftest.py"
    spec = importlib.util.spec_from_file_location("sandbox_conftest_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _send(headers: dict[str, str]) -> None:
    raise httpx.ReadTimeout("slow")


def _failed_call() -> SimpleNamespace:
    try:
        _send({"Authorization": BEARER})
    except httpx.ReadTimeout as error:
        return SimpleNamespace(excinfo=pytest.ExceptionInfo.from_exception(error))
    raise AssertionError("unreachable")


def _run_hook(module: ModuleType, item: SimpleNamespace, call: SimpleNamespace) -> SimpleNamespace:
    report = SimpleNamespace(failed=True, longrepr="original")
    hook = module.pytest_runtest_makereport(item, call)
    next(hook)
    with pytest.raises(StopIteration) as done:
        hook.send(report)
    assert done.value.value is report
    return report


def test_sandbox_failure_report_hides_frame_arguments() -> None:
    module = _sandbox_conftest()
    call = _failed_call()
    control = call.excinfo.getrepr(style="long", funcargs=True, showlocals=True)
    assert BEARER in str(control)
    report = _run_hook(module, SimpleNamespace(path=module.HERE / "test_x.py"), call)
    assert BEARER not in str(report.longrepr)
    assert "ReadTimeout" in str(report.longrepr)
    elsewhere = _run_hook(module, SimpleNamespace(path=Path(__file__)), call)
    assert elsewhere.longrepr == "original"
