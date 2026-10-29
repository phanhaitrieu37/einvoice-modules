import argparse
import sys
from collections.abc import Sequence

from invoice_module.cli import approvals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m invoice_module.cli",
        description="Invoice module operator commands (database only, no provider call).",
    )
    commands = parser.add_subparsers(dest="group", required=True)
    approvals.add_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code (argparse errors return 2)."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
