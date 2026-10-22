"""SePay eInvoice routes and response facts proven on the sandbox.

Every value here is copied from the recorded probe files under
``tests/fixtures/sepay_einvoice/`` (``endpoints.json``, ``findings.json``,
``findings_draft.json``, ``findings_buyer.json``); contract tests pin each copy to its file.
Only the sandbox host is known to this package: a live host must be configured by the
caller (``SePayEInvoiceClient(base_urls=...)``).
"""

from invoice_module.ports.provider import ProviderConfigError

SANDBOX_BASE_URL = "https://einvoice-api-sandbox.sepay.vn"

# Operations the adapter calls, as (method, path). ``None`` marks an operation the probe
# could not verify. Publishing a provider draft is deliberately absent: every invoice is
# created and issued in one step.
ENDPOINTS: dict[str, tuple[str, str] | None] = {
    "token": ("POST", "/v1/token"),
    "provider_accounts": ("GET", "/v1/provider-accounts"),
    "create": ("POST", "/v1/invoices/create"),
    "check_create": ("GET", "/v1/invoices/create/check/{tracking_code}"),
    "get_by_reference": ("GET", "/v1/invoices/{reference_code}"),
    "download": ("GET", "/v1/invoices/{tracking_code}/download?type={pdf|xml}"),
    "quota": ("GET", "/v1/usage"),
}

# Account detail (templates and series). The probe read it for every listed account
# (``provider_accounts.json`` → ``provider_account_details``, HTTP 200 with ``templates``)
# but ``endpoints.json`` has no key for it.
PROVIDER_ACCOUNT_DETAIL: tuple[str, str] = ("GET", "/v1/provider-accounts/{id}")

TOKEN_TTL_SECONDS = 86400
LOOKUP_RETURNS_TOTAL = True
DOWNLOAD_RETURNS = "bytes"

# Where a provider draft shows up (defensive only: this adapter never creates one) and
# where the provider writes the issue date, as dotted paths inside the response body.
DRAFT_MARKER: tuple[str, str] = ("data.invoice.status", "draft")
DRAFT_MARKER_LOOKUP: tuple[str, str] = ("data.status", "draft")
PROVIDER_ISSUED_DATE_PATH = {"check": "data.invoice.issued_date", "lookup": "data.issued_date"}

ADDRESS_RULES = frozenset({"omit", "empty_string"})
# How a blank buyer address is sent: "omit" drops the ``address`` key, "empty_string"
# sends ``""``. The sandbox issued both forms with a null address.
BLANK_ADDRESS_RULE = "omit"


def endpoint(operation: str) -> tuple[str, str]:
    """``(method, path)`` of a verified operation."""
    route = ENDPOINTS.get(operation)
    if route is None:
        raise ProviderConfigError(f"SePay eInvoice operation {operation} is not verified")
    return route


def check_address_rule(rule: str) -> str:
    if rule not in ADDRESS_RULES:
        raise ProviderConfigError(f"unsupported blank address rule: {rule!r}")
    return rule


def check_download_returns(kind: str) -> str:
    if kind != "bytes":
        raise ProviderConfigError(f"unsupported download response kind: {kind!r}")
    return kind


check_address_rule(BLANK_ADDRESS_RULE)
check_download_returns(DOWNLOAD_RETURNS)
