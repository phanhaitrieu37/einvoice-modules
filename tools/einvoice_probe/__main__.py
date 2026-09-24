"""Run the SePay eInvoice sandbox probe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import ProbeClient
from .envfile import load_env_file, preflight
from .scenarios import (
    accounts,
    evidence,
    s1_create_poll,
    s2_duplicate_reference,
    s3_failed_then_retry,
    s4_download,
    s6_quota,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.einvoice_probe")
    parser.add_argument(
        "command", choices=("preflight", "accounts", "s1", "s2", "s3", "s4", "s6", "evidence")
    )
    parser.add_argument("--env", required=True, help="sandbox environment file")
    parser.add_argument("--out", default=".evidence/p0", help="directory for redacted JSON output")
    parser.add_argument(
        "--tax-rate", type=int, help="required for invoice scenarios; provided at sandbox run time"
    )
    args = parser.parse_args()
    env = load_env_file(args.env)
    for line in preflight(env):
        print(line)
    if env.get("SEPAY_EINVOICE_ENVIRONMENT") != "test":
        return 2
    if args.command == "preflight":
        return (
            0
            if all(
                env.get(key)
                for key in (
                    "SEPAY_EINVOICE_SANDBOX_CLIENT_ID",
                    "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET",
                )
            )
            else 2
        )
    needed_tax = args.command in {"s1", "s2", "s3", "s4", "evidence"}
    if needed_tax and args.tax_rate is None:
        parser.error("--tax-rate is required for invoice scenarios")
    output_dir = Path(args.out)
    with ProbeClient(env) as client:
        if args.command == "accounts":
            result = accounts(client)
        elif args.command == "s1":
            result = s1_create_poll(client, tax_rate=args.tax_rate)
        elif args.command == "s2":
            result = s2_duplicate_reference(client, tax_rate=args.tax_rate)
        elif args.command == "s3":
            result = s3_failed_then_retry(client, tax_rate=args.tax_rate)
        elif args.command == "s4":
            result = s4_download(client, tax_rate=args.tax_rate)
        elif args.command == "s6":
            result = s6_quota(client)
        else:
            result = evidence(client, tax_rate=args.tax_rate)
        target = write_json(output_dir, args.command, result)
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
