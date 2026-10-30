"""Operator onboarding of an issuer account: one per merchant and environment, checked against
the provider before it is stored, and switched on or off with an audited status change.

Every accounting setting (template, series, payment method, buyer fallback, cross-day policy)
comes from the accountant through the command; nothing here has a default.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from invoice_module.adapters.sqlalchemy.repositories.issuer_accounts import IssuerAccountRecord
from invoice_module.application.config import today_vn
from invoice_module.domain.document import ALLOWED_FALLBACK_EXTRA_KEYS
from invoice_module.domain.enums import CrossDayAction, Environment, IssuerAccountStatus
from invoice_module.domain.errors import InvoiceError
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.provider import EInvoiceProvider, IssuerAccountView
from invoice_module.ports.unit_of_work import InvoiceUnitOfWorkFactory

logger = logging.getLogger(__name__)

PROVIDER = "sepay_einvoice"
# ``CreateInvoiceRequest.payment_method`` enum of the SePay docs (``xuat-hoa-don-dien-tu``).
PAYMENT_METHODS = frozenset({"TM", "CK", "TM/CK", "KHAC"})
TEMPLATE_CODES = frozenset({"1", "2"})
CREDENTIAL_SCHEME = "env:"


class IssuerAccountMismatch(InvoiceError):
    """The provider does not know the account, template or this year's series."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class IssuerAccountConflict(InvoiceError):
    """The merchant and environment already have an account with other settings."""


class IssuerAccountNotFound(InvoiceError):
    pass


class IssuerAccountNotReady(InvoiceError):
    """The account cannot become active yet; ``reason`` names what is missing."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RegisterIssuerAccountCommand:
    tenant_id: str
    merchant_id: UUID
    environment: Environment
    provider_account_id: str
    template_code: str
    invoice_series_template: str
    payment_method: str
    credential_ref: str
    buyer_fallback_name: str
    buyer_fallback_address: str
    # ``None`` until the accountant decides; the account cannot become active without it.
    issue_date_cross_day_action: CrossDayAction | None
    seller_store_xid: str | None = None
    buyer_fallback_extra: Mapping[str, str] | None = None


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _validated(cmd: RegisterIssuerAccountCommand) -> dict[str, Any]:
    """The row to store, or ``ValueError`` before any database or provider call."""
    _text(cmd.tenant_id, "tenant_id")
    if not isinstance(cmd.merchant_id, UUID):
        raise ValueError("merchant_id must be a UUID")
    environment = Environment(cmd.environment)
    _text(cmd.provider_account_id, "provider_account_id")
    if cmd.template_code not in TEMPLATE_CODES:
        raise ValueError(f"template_code must be one of {sorted(TEMPLATE_CODES)}")
    _text(cmd.invoice_series_template, "invoice_series_template")
    if cmd.payment_method not in PAYMENT_METHODS:
        raise ValueError(f"payment_method must be one of {sorted(PAYMENT_METHODS)}")
    credential_ref = _text(cmd.credential_ref, "credential_ref")
    if not credential_ref.startswith(CREDENTIAL_SCHEME) or not credential_ref.removeprefix(
        CREDENTIAL_SCHEME
    ):
        raise ValueError("credential_ref must name an environment variable as env:NAME")
    _text(cmd.buyer_fallback_name, "buyer_fallback_name")
    if not isinstance(cmd.buyer_fallback_address, str):
        # The accountant may leave the address blank, which is the empty string, never None.
        raise ValueError("buyer_fallback_address must be a string (empty when left blank)")
    action = (
        CrossDayAction(cmd.issue_date_cross_day_action)
        if cmd.issue_date_cross_day_action is not None
        else None
    )
    if cmd.seller_store_xid is not None:
        _text(cmd.seller_store_xid, "seller_store_xid")
    extra: dict[str, str] | None = None
    if cmd.buyer_fallback_extra:
        extra = dict(cmd.buyer_fallback_extra)
        unexpected = extra.keys() - ALLOWED_FALLBACK_EXTRA_KEYS
        if unexpected:
            raise ValueError(f"unsupported buyer fallback fields: {sorted(unexpected)}")
        if not all(isinstance(value, str) for value in extra.values()):
            raise ValueError("buyer fallback fields must be strings")
    return {
        "tenant_id": cmd.tenant_id,
        "merchant_id": cmd.merchant_id,
        "environment": environment,
        "provider": PROVIDER,
        "provider_account_id": cmd.provider_account_id,
        "template_code": cmd.template_code,
        "invoice_series_template": cmd.invoice_series_template,
        "payment_method": cmd.payment_method,
        "credential_ref": credential_ref,
        "seller_store_xid": cmd.seller_store_xid,
        "buyer_fallback_name": cmd.buyer_fallback_name,
        "buyer_fallback_address": cmd.buyer_fallback_address,
        "buyer_fallback_extra": extra,
        "issue_date_cross_day_action": action,
    }


def _same_settings(record: IssuerAccountRecord, values: Mapping[str, Any]) -> bool:
    return all(
        getattr(record, name) == value for name, value in values.items() if name != "provider"
    )


def _provider_view(account_id: UUID, values: Mapping[str, Any]) -> IssuerAccountView:
    fields = IssuerAccountView.__dataclass_fields__
    return IssuerAccountView(
        id=account_id, **{name: value for name, value in values.items() if name in fields}
    )


def _actor(actor: object) -> str:
    return _text(actor, "actor").strip()


class RegisterIssuerAccount:
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

    async def execute(self, cmd: RegisterIssuerAccountCommand, actor: str) -> IssuerAccountRecord:
        """Store the account as ``draft`` once the provider confirmed it.

        The same settings again return the stored account; other settings for the same
        merchant and environment raise ``IssuerAccountConflict``.
        """
        actor = _actor(actor)
        values = _validated(cmd)
        existing = await self._existing(values)
        if existing is not None:
            return existing
        account_id = uuid4()
        await self._verify(_provider_view(account_id, values))
        now = self._clock.now()
        async with self._uow_factory() as uow:
            row = values | {
                "id": account_id,
                "environment": values["environment"].value,
                "issue_date_cross_day_action": (
                    values["issue_date_cross_day_action"].value
                    if values["issue_date_cross_day_action"] is not None
                    else None
                ),
                "status": IssuerAccountStatus.DRAFT.value,
                "created_at": now,
                "updated_at": now,
            }
            inserted = await uow.issuer_accounts.insert_once(row)
            await uow.commit()
        if not inserted:
            # Another operator registered the same merchant and environment meanwhile.
            existing = await self._existing(values)
            assert existing is not None
            return existing
        logger.info(
            "invoice_issuer_account_registered",
            extra={
                "account_id": str(account_id),
                "tenant_id": cmd.tenant_id,
                "environment": values["environment"].value,
                "actor": actor,
            },
        )
        async with self._uow_factory() as uow:
            record = await uow.issuer_accounts.get_record(account_id)
        assert record is not None
        return record

    async def _existing(self, values: Mapping[str, Any]) -> IssuerAccountRecord | None:
        async with self._uow_factory() as uow:
            record = await uow.issuer_accounts.get_by_scope(
                values["tenant_id"], values["merchant_id"], values["environment"]
            )
        if record is None:
            return None
        if not _same_settings(record, values):
            raise IssuerAccountConflict(
                "the merchant already has an issuer account in this environment"
            )
        return record

    async def _verify(self, account: IssuerAccountView) -> None:
        creds = await self._credentials.client_credentials(account.credential_ref)
        facts = await self._provider.describe_account(account, creds)
        if facts.provider_account_id != account.provider_account_id:
            raise IssuerAccountMismatch("provider_account_id")
        if account.template_code not in facts.templates:
            raise IssuerAccountMismatch("template_code")
        year = today_vn(self._clock.now()).year
        series = account.invoice_series_template.replace("{YY}", f"{year % 100:02d}")
        if series not in facts.series:
            raise IssuerAccountMismatch("invoice_series")


class SetIssuerAccountStatus:
    def __init__(self, uow_factory: InvoiceUnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(
        self, account_id: UUID, status: IssuerAccountStatus, actor: str, reason: str
    ) -> IssuerAccountRecord:
        target = IssuerAccountStatus(status)
        actor = _actor(actor)
        reason = _text(reason, "reason")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            record = await uow.issuer_accounts.get_record(account_id, for_update=True)
            if record is None:
                raise IssuerAccountNotFound(str(account_id))
            if target == IssuerAccountStatus.ACTIVE:
                if record.issue_date_cross_day_action is None:
                    raise IssuerAccountNotReady("issue_date_cross_day_action")
                if record.environment == Environment.LIVE and record.sandbox_evidence_ref is None:
                    raise IssuerAccountNotReady("sandbox_evidence_ref")
            await uow.issuer_accounts.set_status(account_id, target, now)
            await uow.commit()
        logger.info(
            "invoice_issuer_account_status_changed",
            extra={
                "account_id": str(account_id),
                "from_status": record.status.value,
                "to_status": target.value,
                "actor": actor,
                "reason": reason,
            },
        )
        async with self._uow_factory() as uow:
            updated = await uow.issuer_accounts.get_record(account_id)
        assert updated is not None
        return updated
