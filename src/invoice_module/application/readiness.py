"""Light readiness gate before an issuer account goes live: the provider reports the account as
active, and a sandbox evidence file shows the scenario run passed.

The file is checked for what it says, not signed or dated: it exists, comes from the ``test``
environment, and scenarios s1, s2 and s4 passed; s3 passed or is ``INCONCLUSIVE`` with an
explained variant (the sandbox cannot be made to fail a job asynchronously).
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from invoice_module.application.config import today_vn
from invoice_module.application.issuer_accounts import IssuerAccountNotFound
from invoice_module.domain.enums import Environment
from invoice_module.domain.errors import InvoiceError
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.provider import EInvoiceProvider
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

PASS = "PASS"
INCONCLUSIVE = "INCONCLUSIVE"
REQUIRED_PASS = ("s1", "s2", "s4")
EXPLAINED_S3_VARIANTS = frozenset({"not_triggerable", "sync_reject"})


class ReadinessRejected(InvoiceError):
    """``reason`` is one of ``account_inactive``, ``missing_file``, ``wrong_environment`` or
    ``scenario_not_pass``."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ReadinessResult:
    account_id: UUID
    warnings: tuple[str, ...]


def _read(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReadinessRejected("missing_file") from exc
    try:
        return json.loads(text)
    except ValueError:
        return None  # not JSON: it cannot show the test environment


def _check_evidence(evidence: Any) -> None:
    if not isinstance(evidence, dict) or evidence.get("environment") != Environment.TEST.value:
        raise ReadinessRejected("wrong_environment")
    scenarios = evidence.get("scenarios")
    details = evidence.get("details")
    if not isinstance(scenarios, dict):
        raise ReadinessRejected("scenario_not_pass")
    if any(scenarios.get(name) != PASS for name in REQUIRED_PASS):
        raise ReadinessRejected("scenario_not_pass")
    s3 = scenarios.get("s3")
    variant = details.get("s3_variant") if isinstance(details, dict) else None
    if s3 != PASS and not (s3 == INCONCLUSIVE and variant in EXPLAINED_S3_VARIANTS):
        raise ReadinessRejected("scenario_not_pass")


class RecordIssuerReadiness:
    def __init__(
        self,
        uow_factory: InvoiceUnitOfWorkFactory,
        provider: EInvoiceProvider,
        credentials: CredentialResolver,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._credentials = credentials
        self._clock = clock

    async def execute(self, account_id: UUID, evidence_ref: str, actor: str) -> ReadinessResult:
        """Record ``evidence_ref`` (a path to the sandbox evidence JSON) on the account.

        Warnings never block: a live account before (or without a reported) tax authority
        approval date is recorded with ``before_tax_approval_date`` or
        ``approval_date_unknown``.
        """
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor is required")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("evidence_ref is required")
        async with self._uow_factory() as uow:
            account = await uow.issuer_accounts.get_view(account_id)
        if account is None:
            raise IssuerAccountNotFound(str(account_id))
        creds = await self._credentials.client_credentials(account.credential_ref)
        facts = await self._provider.describe_account(account, creds)
        # Docs ``bat-dau-nhanh`` ("Chọn tài khoản đúng"): "Chỉ sử dụng tài khoản có active: true".
        if facts.active is not True:
            raise ReadinessRejected("account_inactive")
        warnings: list[str] = []
        if account.environment == Environment.LIVE:
            # Production refuses an issue date before this date (422
            # INVOICE_DATE_BEFORE_TAX_APPROVAL); the sandbox reports null and skips the check.
            approved = facts.tax_authority_approved_date
            if approved is None:
                warnings.append("approval_date_unknown")
            elif today_vn(self._clock.now()) < approved:
                warnings.append("before_tax_approval_date")
        _check_evidence(_read(Path(evidence_ref).expanduser()))
        now = self._clock.now()
        async with self._uow_factory() as uow:
            await uow.issuer_accounts.record_evidence(account_id, evidence_ref, now)
            await uow.commit()
        for warning in warnings:
            logger.warning(
                "invoice_issuer_readiness_warning",
                extra={"account_id": str(account_id), "warning": warning},
            )
        logger.info(
            "invoice_issuer_readiness_recorded",
            extra={"account_id": str(account_id), "actor": actor, "warnings": warnings},
        )
        return ReadinessResult(account_id, tuple(warnings))
