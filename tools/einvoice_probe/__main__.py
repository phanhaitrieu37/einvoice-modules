"""Run the SePay eInvoice sandbox probe."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .client import ProbeClient
from .envfile import load_env_file, preflight
from .scenarios import (
    S8_PHASES,
    SameDayError,
    accounts,
    error_401,
    error_422,
    evidence,
    s1_create_poll,
    s2_duplicate_reference,
    s3_failed_then_retry,
    s4_download,
    s6_quota,
    s7_draft_then_issue,
    s8_draft_cross_day,
    s9_fallback_buyer_address,
    tax_matrix,
    write_json,
)


def _issued_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD HH:mm:ss") from error
    return value


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.einvoice_probe")
    parser.add_argument(
        "command",
        choices=(
            "preflight",
            "accounts",
            "s1",
            "s2",
            "s3",
            "s4",
            "s6",
            "s7",
            "s8",
            "s9",
            "evidence",
            "error_401",
            "error_422",
            "tax_matrix",
        ),
    )
    parser.add_argument("--env", required=True, help="sandbox environment file")
    parser.add_argument("--out", default=".evidence/p0", help="directory for redacted JSON output")
    parser.add_argument(
        "--tax-rate", type=int, help="required integer tax rate; supplied by the user"
    )
    parser.add_argument(
        "--issued-date",
        type=_issued_date,
        help="required YYYY-MM-DD HH:mm:ss; supplied by the user",
    )
    parser.add_argument(
        "--phase", choices=S8_PHASES, help="s8 only: create drafts, issue them, or backdate"
    )
    parser.add_argument(
        "--references-file", help="s8 only: JSON file holding the draft references between days"
    )
    parser.add_argument(
        "--draft-issued-date",
        type=_issued_date,
        help="s8 --phase backdate only: past YYYY-MM-DD HH:mm:ss for the draft",
    )
    parser.add_argument(
        "--buyer-name", help="s9 only: fallback buyer name sent with no other buyer field"
    )
    parser.add_argument("--template-code", help="select a template when the account offers several")
    parser.add_argument("--invoice-series", help="select a series when the account offers several")
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
    needed_tax = args.command in {"s1", "s2", "s3", "s4", "s7", "s9", "evidence"} or (
        args.command == "s8" and args.phase in {"create", "backdate"}
    )
    if needed_tax and args.tax_rate is None:
        parser.error("--tax-rate is required for invoice scenarios")
    if (needed_tax or args.command == "s8") and args.issued_date is None:
        parser.error("--issued-date is required for invoice scenarios")
    if args.command == "s9" and not args.buyer_name:
        parser.error("--buyer-name is required for s9")
    if args.command == "s8":
        if args.phase is None:
            parser.error("--phase is required for s8")
        if args.phase in {"create", "issue"} and not args.references_file:
            parser.error("--references-file is required for s8 --phase create|issue")
        if args.phase == "backdate" and args.draft_issued_date is None:
            parser.error("--draft-issued-date is required for s8 --phase backdate")
    output_dir = Path(args.out)
    with ProbeClient(env) as client:
        if args.command == "accounts":
            result = accounts(client, output_dir)
        elif args.command == "s1":
            result = s1_create_poll(
                client,
                tax_rate=args.tax_rate,
                issued_date=args.issued_date,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
            )
        elif args.command == "s2":
            result = s2_duplicate_reference(
                client,
                tax_rate=args.tax_rate,
                issued_date=args.issued_date,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
            )
        elif args.command == "s3":
            result = s3_failed_then_retry(
                client,
                tax_rate=args.tax_rate,
                issued_date=args.issued_date,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
            )
        elif args.command == "s4":
            result = s4_download(
                client,
                tax_rate=args.tax_rate,
                issued_date=args.issued_date,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
            )
        elif args.command == "s6":
            result = s6_quota(client)
        elif args.command == "s7":
            result = s7_draft_then_issue(
                client,
                tax_rate=args.tax_rate,
                issued_date=args.issued_date,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
                out_dir=output_dir,
            )
        elif args.command == "s8":
            try:
                result = s8_draft_cross_day(
                    client,
                    phase=args.phase,
                    tax_rate=args.tax_rate,
                    issued_date=args.issued_date,
                    references_file=args.references_file,
                    draft_issued_date=args.draft_issued_date,
                    template_code=args.template_code,
                    invoice_series=args.invoice_series,
                    out_dir=output_dir,
                )
            except (SameDayError, FileExistsError) as refused:
                print(refused, file=sys.stderr)
                return 2
        elif args.command == "s9":
            result = s9_fallback_buyer_address(
                client,
                tax_rate=args.tax_rate,
                issued_date=args.issued_date,
                buyer_name=args.buyer_name,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
                out_dir=output_dir,
            )
        elif args.command == "error_401":
            result = error_401(client)
        elif args.command == "error_422":
            result = error_422(client)
        elif args.command == "tax_matrix":
            if args.issued_date is None:
                parser.error("--issued-date is required for tax_matrix")
            result = tax_matrix(
                client,
                issued_date=args.issued_date,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
            )
        else:

            def save_scenario(name: str, result: dict[str, object]) -> None:
                write_json(output_dir, f"evidence_{name}", result)

            result = evidence(
                client,
                tax_rate=args.tax_rate,
                issued_date=args.issued_date,
                template_code=args.template_code,
                invoice_series=args.invoice_series,
                on_result=save_scenario,
            )
        name = f"s8-{args.phase}" if args.command == "s8" else args.command
        target = write_json(output_dir, name, result)
        print(f"wrote {target}")
    if isinstance(result, dict) and result.get("stopped"):
        print(f"stopped: {result['stopped']}", file=sys.stderr)
        return 3
    if args.command == "s8" and args.phase == "create" and not result.get("references_written"):
        print("references were not written: a draft was not created", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
