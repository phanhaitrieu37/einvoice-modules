"""HTTP client for SePay eInvoice: token cache, sandbox-only ``test`` host, pacing, errors.

A create (POST) is sent at most once per call and is never resent by this client: a
redirect is not followed and a 401 only drops the cached token so that the next call
fetches a new one. ``before_send`` runs exactly once per call, after the token and the rate
limiter and immediately before the transport, so a hook that records the right to POST
corresponds to at most one request on the wire.

``http_timeout_s`` only ends the client's wait. It does not prove that the provider dropped
the request; a document whose outcome is unclear stays for the operator to reconcile and
its reference is never changed here.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from invoice_module.adapters.sepay.endpoints import SANDBOX_BASE_URL, TOKEN_TTL_SECONDS, endpoint
from invoice_module.adapters.sepay.errors import map_error
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from invoice_module.adapters.sepay.redact import redact
from invoice_module.domain.enums import Environment
from invoice_module.ports.clock import Clock
from invoice_module.ports.credentials import CredentialResolver
from invoice_module.ports.provider import (
    BeforeSend,
    ClientCredentials,
    IssuerAccountView,
    ProviderAuthFailed,
    ProviderConfigError,
    ProviderTransient,
    SendAborted,
)

logger = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(minutes=5)
OK_STATUSES = range(200, 300)


@dataclass(frozen=True)
class _Token:
    value: str = ""
    expires_at: datetime | None = None

    def __repr__(self) -> str:
        return "_Token(<hidden>)"


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: Mapping[str, str]


class SePayEInvoiceClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        credentials: CredentialResolver,
        clock: Clock,
        limiter: ProcessRateLimiter,
        base_urls: Mapping[Environment, str] | None = None,
        http_timeout_s: float = 20.0,
    ) -> None:
        urls = dict(base_urls) if base_urls is not None else {Environment.TEST: SANDBOX_BASE_URL}
        if urls.get(Environment.TEST, SANDBOX_BASE_URL) != SANDBOX_BASE_URL:
            raise ProviderConfigError("the test environment must use the SePay sandbox")
        urls.setdefault(Environment.TEST, SANDBOX_BASE_URL)
        self._http = http
        self._credentials = credentials
        self._clock = clock
        self._limiter = limiter
        self._base_urls = urls
        self._timeout = http_timeout_s
        self._tokens: dict[str, _Token] = {}

    def base_url_for(self, environment: Environment) -> str:
        url = self._base_urls.get(Environment(environment))
        if url is None:
            raise ProviderConfigError(f"no SePay eInvoice URL configured for {environment}")
        return url.rstrip("/")

    def invalidate(self, credential_ref: str) -> None:
        self._tokens.pop(credential_ref, None)

    async def request(
        self,
        method: str,
        path: str,
        *,
        account: IssuerAccountView,
        creds: ClientCredentials | None = None,
        json: Mapping[str, object] | None = None,
        before_send: BeforeSend | None = None,
        accept: tuple[int, ...] = (),
        log_body: bool = True,
    ) -> Response:
        """Send one API call and return a success (or ``accept``-listed) response.

        Any other status is raised as a port error. ``SendAborted`` means no request was
        sent because ``before_send`` returned ``False``.
        """
        url = self.base_url_for(account.environment) + path
        token, fresh = await self._token(account, creds)
        await self._limiter.acquire()
        if before_send is not None and not await before_send():
            raise SendAborted()
        response = await self._send(method, url, token, json)
        if response.status == 401:
            self.invalidate(account.credential_ref)
            if method != "GET":
                # Never resend a create: the next call starts with a new token.
                if fresh:
                    raise ProviderAuthFailed("token rejected")
                raise ProviderTransient(retry_after=None)
            token, _ = await self._token(account, creds)
            await self._limiter.acquire()
            response = await self._send(method, url, token, json)
            if response.status == 401:
                self.invalidate(account.credential_ref)
                raise ProviderAuthFailed("token rejected")
        self._log(method, path, response, log_body)
        if response.status in OK_STATUSES or response.status in accept:
            return response
        raise map_error(response.status, response.body, response.headers)

    async def _token(
        self, account: IssuerAccountView, creds: ClientCredentials | None
    ) -> tuple[str, bool]:
        ref = account.credential_ref
        cached = self._tokens.get(ref)
        now = self._clock.now()
        if (
            cached is not None
            and cached.expires_at is not None
            and now < cached.expires_at - REFRESH_MARGIN
        ):
            return cached.value, False
        if creds is None:
            creds = await self._credentials.client_credentials(ref)
        method, path = endpoint("token")
        await self._limiter.acquire()
        try:
            raw = await self._http.request(
                method,
                self.base_url_for(account.environment) + path,
                auth=httpx.BasicAuth(creds.client_id, creds.client_secret),
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise ProviderTransient(retry_after=None) from error
        response = _response(raw)
        if response.status not in OK_STATUSES:
            logger.warning("sepay einvoice token request failed with HTTP %s", response.status)
            raise map_error(response.status, response.body, response.headers)
        data = response.body.get("data") if isinstance(response.body, Mapping) else None
        value = data.get("access_token") if isinstance(data, Mapping) else None
        if not isinstance(value, str) or not value:
            raise ProviderAuthFailed("token response without an access token")
        ttl = data.get("expires_in")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            ttl = TOKEN_TTL_SECONDS
        self._tokens[ref] = _Token(value, now + timedelta(seconds=ttl))
        return value, True

    async def _send(
        self, method: str, url: str, token: str, body: Mapping[str, object] | None
    ) -> Response:
        try:
            raw = await self._http.request(
                method,
                url,
                json=dict(body) if body is not None else None,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            logger.warning("sepay einvoice %s request failed: %s", method, type(error).__name__)
            raise ProviderTransient(retry_after=None) from error
        return _response(raw)

    def _log(self, method: str, path: str, response: Response, log_body: bool) -> None:
        level = logging.DEBUG if response.status in OK_STATUSES else logging.WARNING
        if log_body:
            logger.log(
                level,
                "sepay einvoice %s %s -> HTTP %s %s",
                method,
                path,
                response.status,
                redact(response.body),
            )
        else:
            logger.log(level, "sepay einvoice %s %s -> HTTP %s", method, path, response.status)


def _response(raw: httpx.Response) -> Response:
    try:
        body: Any = json.loads(raw.content, parse_float=Decimal) if raw.content else None
    except ValueError:
        body = None
    return Response(status=raw.status_code, body=body, headers=dict(raw.headers))
