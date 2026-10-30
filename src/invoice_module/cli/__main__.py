import argparse
import re
import sys
from collections.abc import Sequence
from typing import NoReturn

from invoice_module.cli import approvals

PROG = "python -m invoice_module.cli"
HIDDEN = "<hidden>"
# A value that cannot hold a database URL or a password stays readable in a usage error.
_PLAIN_VALUE = re.compile(r"[\w.-]+")
# An option name as typed before any ``=``; a dash token that is not one (``-xuser:pw@host``)
# is a typed value as a whole.
_OPTION_NAME = re.compile(r"-[\w-]*")
_URL_LIKE = re.compile(r"\S*://\S*")


class _UsageFailure(Exception):
    def __init__(self, usage: str, message: str) -> None:
        self.usage, self.message = usage, message
        super().__init__(message)


class _Parser(argparse.ArgumentParser):
    """argparse echoes what was typed in its errors (a misspelt ``--database-url`` with the
    URL after it, a stray positional): the error is raised to ``main``, which hides it."""

    def error(self, message: str) -> NoReturn:
        raise _UsageFailure(self.format_usage(), message)


def _typed_value(token: str) -> str:
    if not token.startswith("-"):
        return token
    name, _, value = token.partition("=")
    return value if _OPTION_NAME.fullmatch(name) else token


def _hide_typed_values(message: str, argv: Sequence[str]) -> str:
    values = {_typed_value(token) for token in argv}
    # argparse quotes a wrong value with repr(), which escapes backslashes and quotes, so the
    # quoted form is hidden as well as the typed one.
    forms = {
        form
        for value in values
        if value and not _PLAIN_VALUE.fullmatch(value)
        for form in (repr(value), value)
    }
    for form in sorted(forms, key=len, reverse=True):
        message = message.replace(form, HIDDEN)
    return _URL_LIKE.sub(HIDDEN, message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description="Invoice module operator commands (database only, no provider call).",
    )
    commands = parser.add_subparsers(dest="group", required=True)
    approvals.add_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code (argparse errors return 2).

    A usage error names the options but never repeats a typed value that could hold the
    database URL or its password.
    """
    typed = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(typed)
    except _UsageFailure as exc:
        print(exc.usage, end="", file=sys.stderr)
        print(f"{PROG}: error: {_hide_typed_values(exc.message, typed)}", file=sys.stderr)
        return approvals.EXIT_USAGE
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
