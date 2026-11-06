from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from invoice_module.domain.document import InvoiceDocument
from invoice_module.domain.enums import CrossDayAction, Environment

BeforeSend = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class IssuerAccountView:
    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: Environment
    provider_account_id: str
    template_code: str
    invoice_series_template: str
    payment_method: str
    seller_store_xid: str | None
    credential_ref: str
    buyer_fallback_name: str
    buyer_fallback_address: str
    buyer_fallback_extra: Mapping[str, str] | None
    issue_date_cross_day_action: CrossDayAction | None


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class Accepted:
    tracking_code: str | None


# "draft" is defensive only: invoices are always created and issued in one step, so a
# draft under one of our references is an anomaly the caller sends to review.
# "cancelled" is a lookup result: the provider reports the invoice as cancelled.
ProviderState = Literal["pending", "success", "failed", "draft", "cancelled"]


@dataclass(frozen=True)
class ProviderProgress:
    state: ProviderState
    invoice_number: str | None = None
    tax_authority_code: str | None = None
    provider_invoice_id: str | None = None
    next_retry_at: datetime | None = None
    error_code: str | None = None
    issued_date: date | None = None
    provider_message: str | None = None


@dataclass(frozen=True)
class ProviderInvoice:
    reference_code: str
    state: ProviderState
    tracking_code: str | None = None
    provider_invoice_id: str | None = None
    invoice_number: str | None = None
    tax_authority_code: str | None = None
    total_amount_vnd: int | None = None
    issued_date: date | None = None
    provider_message: str | None = None


@dataclass(frozen=True)
class Artifacts:
    pdf: bytes
    xml: bytes


@dataclass(frozen=True)
class AccountFacts:
    provider_account_id: str
    templates: tuple[str, ...]
    series: tuple[str, ...]
    quota_remaining: int | None = None
    # Provider account status: only an active account may issue, and the provider refuses
    # an issue date before the tax authority approval date (``None`` when not reported).
    active: bool | None = None
    tax_authority_approved_date: date | None = None


class EInvoiceProvider(Protocol):
    async def create(
        self,
        account: IssuerAccountView,
        creds: ClientCredentials,
        request: Mapping[str, object],
        *,
        before_send: BeforeSend | None = None,
    ) -> Accepted: ...

    def build_create_payload(
        self,
        account: IssuerAccountView,
        doc: InvoiceDocument,
        reference_code: str,
        issued_date: date,
    ) -> dict[str, object]:
        """Build a create request that issues in one step (never a provider draft).

        ``issued_date`` is the Asia/Saigon date the caller froze just before sending.
        """
        ...

    async def check_create(
        self, account: IssuerAccountView, creds: ClientCredentials, tracking_code: str
    ) -> ProviderProgress | None:
        """The job of ``tracking_code``; ``None`` when the provider has no such job."""
        ...

    async def find_by_reference(
        self, account: IssuerAccountView, creds: ClientCredentials, reference_code: str
    ) -> ProviderInvoice | None: ...

    async def adjust(
        self, account: IssuerAccountView, creds: ClientCredentials, request: Mapping[str, object]
    ) -> Accepted: ...

    async def replace(
        self, account: IssuerAccountView, creds: ClientCredentials, request: Mapping[str, object]
    ) -> Accepted: ...

    def build_adjust_payload(self, *args: object, **kwargs: object) -> dict[str, object]: ...

    def build_replace_payload(self, *args: object, **kwargs: object) -> dict[str, object]: ...

    async def fetch_artifacts(
        self,
        account: IssuerAccountView,
        creds: ClientCredentials,
        reference_code: str,
        provider_invoice_id: str | None,
    ) -> Artifacts:
        """PDF and XML of the invoice under ``reference_code``; ``provider_invoice_id`` is
        ``None`` when the provider never reported one."""
        ...

    async def describe_account(
        self, account: IssuerAccountView, creds: ClientCredentials
    ) -> AccountFacts: ...


class ProviderError(Exception):
    pass


class ProviderTransient(ProviderError):
    def __init__(self, retry_after: timedelta | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("transient provider error")


class ProviderRejected(ProviderError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class QuotaExhausted(ProviderError):
    pass


class DocumentExists(ProviderError):
    pass


class ProviderAuthFailed(ProviderError):
    pass


class ProviderConfigError(ProviderError):
    pass


class SendAborted(ProviderError):
    """No provider request was sent because the lease was no longer valid."""
