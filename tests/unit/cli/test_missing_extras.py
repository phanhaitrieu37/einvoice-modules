"""Installed without the database extras, the command line prints one install hint and exits 1
instead of a traceback."""

import importlib
import subprocess
import sys

import pytest

import invoice_module.cli

MAIN = "invoice_module.cli.__main__"
TYPED_URL = "postgresql+asyncpg://synuser:SYNTHPW0MARKER@127.0.0.1:5433/postgres"
# Runs the command line as ``python -m invoice_module.cli`` would, with SQLAlchemy missing.
WITHOUT_SQLALCHEMY = (
    "import runpy, sys; sys.modules['sqlalchemy'] = None; "
    "runpy.run_module('invoice_module.cli', run_name='__main__', alter_sys=True)"
)


def fresh_import(monkeypatch: pytest.MonkeyPatch, missing: str) -> object:
    """Import the entry point as a fresh process would, whatever this session imported."""
    monkeypatch.setitem(sys.modules, missing, None)
    monkeypatch.delitem(sys.modules, MAIN, raising=False)
    monkeypatch.delitem(sys.modules, "invoice_module.cli.approvals", raising=False)
    # ``from invoice_module.cli import approvals`` reads the package attribute first.
    monkeypatch.delattr(invoice_module.cli, "approvals", raising=False)
    return importlib.import_module(MAIN)


def test_missing_database_extras_print_the_install_hint(monkeypatch, capsys):
    with pytest.raises(SystemExit) as raised:
        fresh_import(monkeypatch, "sqlalchemy")
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "python -m invoice_module.cli: error: the operator commands need the database extras; "
        "install invoice-module[sqlalchemy,postgres]\n"
    )


def test_a_missing_module_of_the_package_itself_is_not_hidden(monkeypatch):
    with pytest.raises(ModuleNotFoundError) as raised:
        fresh_import(monkeypatch, "invoice_module.builder")
    assert raised.value.name == "invoice_module.builder"


@pytest.mark.parametrize("argv", [["--help"], ["approvals", "list", "--database-url", TYPED_URL]])
def test_module_entry_point_without_extras_shows_no_traceback(argv):
    result = subprocess.run(
        [sys.executable, "-I", "-c", WITHOUT_SQLALCHEMY, *argv],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "python -m invoice_module.cli: error: the operator commands need the database extras; "
        "install invoice-module[sqlalchemy,postgres]"
    ]
