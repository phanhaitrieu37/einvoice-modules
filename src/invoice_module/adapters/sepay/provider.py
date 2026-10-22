"""``EInvoiceProvider`` for SePay eInvoice (v0.1: original invoices, issued in one step)."""

import base64
import binascii
from collections.abc import Mapping
from datetime import date
from urllib.parse import quote

from invoice_module.adapters.sepay import payload
from invoice_module.adapters.sepay.client import Response, SePayEInvoiceClient
from invoice_module.adapters.sepay.endpoints import ENDPOINTS, PROVIDER_ACCOUNT_DETAIL, endpoint
from invoice_module.adapters.sepay.errors import error_code, map_error
from invoice_module.domain.document import InvoiceDocument
from invoice_module.ports.provider import (
    Accepted,
    AccountFacts,
    Artifacts,
    BeforeSend,
    ClientCredentials,
    IssuerAccountView,
    ProviderConfigError,
    ProviderError,
    ProviderInvoice,
    ProviderProgress,
    ProviderRejected,
    ProviderTransient,
)

NOT_FOUND = 404
ARTIFACT_KINDS = ("pdf", "xml")
PDF_MAGIC = b"%PDF"
# Largest page of provider accounts the API serves (docs ``danh-sach-tai-khoan``).
ACCOUNTS_PER_PAGE = 100


def _path(template: str, **values: str) -> str:
    for name, value in values.items():
        template = template.replace("{" + name + "}", quote(value, safe=""))
    return template


class SePayEInvoiceProvider:
    def __init__(self, client: SePayEInvoiceClient) -> None:
        self._client = client

    def build_create_payload(
        self,
        account: IssuerAccountView,
        doc: InvoiceDocument,
        reference_code: str,
        issued_date: date,
    ) -> dict[str, object]:
        return payload.build_create_payload(account, doc, reference_code, issued_date)

    async def create(
        self,
        account: IssuerAccountView,
        creds: ClientCredentials,
        request: Mapping[str, object],
        *,
        before_send: BeforeSend | None = None,
    ) -> Accepted:
        """Send ``request`` exactly as frozen by the caller; never resent by the adapter.

        Only ``success: true`` with a tracking code is an acceptance. Any other 2xx is an
        error: a provider error code maps like an error response, and a body without one
        (HTML, empty, 204) is transient, so the caller looks the reference up first.
        """
        method, path = endpoint("create")
        response = await self._client.request(
            method, path, account=account, creds=creds, json=request, before_send=before_send
        )
        accepted = payload.parse_accepted(response.body)
        if accepted is None:
            raise _not_accepted(response)
        return accepted

    async def check_create(
        self, account: IssuerAccountView, creds: ClientCredentials, tracking_code: str
    ) -> ProviderProgress:
        method, template = endpoint("check_create")
        response = await self._client.request(
            method, _path(template, tracking_code=tracking_code), account=account, creds=creds
        )
        return payload.parse_progress(response.body)

    async def find_by_reference(
        self, account: IssuerAccountView, creds: ClientCredentials, reference_code: str
    ) -> ProviderInvoice | None:
        """Lookup by reference only; ``None`` when the provider has no such reference.

        "No such reference" is a 404 carrying the provider error envelope (docs: "Trả về
        404: chưa có hóa đơn nào được ghi nhận"); any other 404 (HTML, empty) is transient.
        A missing invoice number or issue date stays ``None`` on the result.
        """
        method, template = endpoint("get_by_reference")
        response = await self._client.request(
            method,
            _path(template, reference_code=reference_code),
            account=account,
            creds=creds,
            accept=(NOT_FOUND,),
        )
        if response.status == NOT_FOUND:
            if _is_error_envelope(response.body):
                return None
            raise ProviderTransient(retry_after=None)
        return payload.parse_invoice(response.body, reference_code)

    async def fetch_artifacts(
        self,
        account: IssuerAccountView,
        creds: ClientCredentials,
        reference_code: str,
        provider_invoice_id: str,
    ) -> Artifacts:
        """PDF and XML as bytes (the download returns base64 ``data.content``).

        The download is addressed by tracking code, which equals the reference code of a
        create (recorded create and check responses); ``provider_invoice_id`` is unused
        because no SePay response carries such an id.
        """
        method, template = endpoint("download")
        files: dict[str, bytes] = {}
        for kind in ARTIFACT_KINDS:
            path = _path(template, tracking_code=reference_code).replace("{pdf|xml}", kind)
            response = await self._client.request(
                method,
                path,
                account=account,
                creds=creds,
                timeout_s=self._client.artifact_timeout_s,
            )
            files[kind] = _decode_artifact(response.body, kind)
        return Artifacts(pdf=files["pdf"], xml=files["xml"])

    async def provider_account_ids(
        self, account: IssuerAccountView, creds: ClientCredentials | None = None
    ) -> tuple[str, ...]:
        """Ids of the active provider accounts visible to the credentials of ``account``.

        Reads one page of the largest size; more accounts than that is a configuration the
        adapter does not page through.
        """
        method, path = endpoint("provider_accounts")
        response = await self._client.request(
            method, f"{path}?per_page={ACCOUNTS_PER_PAGE}", account=account, creds=creds
        )
        data = payload.response_data(response.body)
        paging = data.get("paging")
        if isinstance(paging, Mapping) and paging.get("has_more") is True:
            raise ProviderConfigError(
                f"more than {ACCOUNTS_PER_PAGE} SePay eInvoice provider accounts are listed"
            )
        items = data.get("items")
        return tuple(
            str(item["id"])
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, Mapping) and item.get("id") and item.get("active") is True
        )

    async def describe_account(
        self, account: IssuerAccountView, creds: ClientCredentials
    ) -> AccountFacts:
        method, template = PROVIDER_ACCOUNT_DETAIL
        detail = await self._client.request(
            method, _path(template, id=account.provider_account_id), account=account, creds=creds
        )
        quota_body = None
        if ENDPOINTS["quota"] is not None:
            quota_method, quota_path = ENDPOINTS["quota"]
            quota = await self._client.request(
                quota_method, quota_path, account=account, creds=creds
            )
            quota_body = quota.body
        return payload.parse_account_facts(account.provider_account_id, detail.body, quota_body)

    async def adjust(
        self, account: IssuerAccountView, creds: ClientCredentials, request: Mapping[str, object]
    ) -> Accepted:
        raise NotImplementedError("v0.2")

    async def replace(
        self, account: IssuerAccountView, creds: ClientCredentials, request: Mapping[str, object]
    ) -> Accepted:
        raise NotImplementedError("v0.2")

    def build_adjust_payload(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise NotImplementedError("v0.2")

    def build_replace_payload(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise NotImplementedError("v0.2")


def _is_error_envelope(body: object) -> bool:
    return (
        isinstance(body, Mapping) and body.get("success") is False and error_code(body) is not None
    )


def _not_accepted(response: Response) -> ProviderError:
    if error_code(response.body) is not None:
        return map_error(max(response.status, 400), response.body, response.headers)
    return ProviderTransient(retry_after=None)


def _decode_artifact(body: object, kind: str) -> bytes:
    data = payload.response_data(body)
    content = data.get("content")
    if data.get("file_type") not in (None, kind) or not isinstance(content, str) or not content:
        raise ProviderRejected(f"artifact_{kind}_missing")
    try:
        decoded = base64.b64decode(content.replace("\r", "").replace("\n", ""), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ProviderRejected(f"artifact_{kind}_invalid") from error
    if not decoded:
        raise ProviderRejected(f"artifact_{kind}_missing")
    if kind == "pdf" and not decoded.startswith(PDF_MAGIC):
        raise ProviderRejected("artifact_pdf_invalid")
    return decoded
