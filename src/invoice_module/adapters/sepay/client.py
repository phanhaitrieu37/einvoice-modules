"""HTTP client for SePay eInvoice: token cache, sandbox-only ``test`` host, pacing, errors.

A create (POST) is sent at most once per call and is never resent by this client: a
redirect is not followed and a 401 only drops the cached token so that the next call
fetches a new one. ``before_send`` runs exactly once per call, after the token and the rate
limiter and immediately before the transport, so a hook that records the right to POST
corresponds to at most one request on the wire.

``http_timeout_s`` only ends the client's wait. It does not prove that the provider dropped
the request; a document whose outcome is unclear stays for the operator to reconcile and
its reference is never changed here.

The access token travels only inside ``_Token`` and ``_BearerAuth``, whose reprs hide it, so
no frame argument or local of a failure traceback shows it. Only a token of the RFC 6750
grammar is used, and a refused request header ends in a port error without the library
error chained, because that error's message quotes the header. Logs carry the method, path,
HTTP status, ``error.code``, ``data.status`` and the length of the provider message, never a
body or provider text (the full message is kept by the caller, not logged here).

The ``httpx.AsyncClient`` is the host's: its own mounts, proxies or event hooks decide where
a request really goes, so the sandbox guard here checks the configured base URLs only.
"""

import json
import logging
import re
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

import httpx

from invoice_module.adapters.sepay.endpoints import SANDBOX_BASE_URL, TOKEN_TTL_SECONDS, endpoint
from invoice_module.adapters.sepay.errors import error_code, map_error
from invoice_module.adapters.sepay.payload import bounded_decimal
from invoice_module.adapters.sepay.rate_limiter import ProcessRateLimiter
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
# A token lifetime outside (0, one year] is not believed; the documented 24 h is used.
MAX_TOKEN_TTL_SECONDS = 365 * 86400
# RFC 6750 ``b64token``, the only characters a bearer token may carry in the header.
ACCESS_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


@dataclass(frozen=True)
class _Token:
    """An access token; its repr never shows the value (tracebacks, logs)."""

    value: str = ""
    expires_at: datetime | None = None

    def __repr__(self) -> str:
        return "_Token(<hidden>)"


class _BearerAuth(httpx.Auth):
    """Adds ``Authorization: Bearer <token>`` at send time; the repr never shows the token."""

    def __init__(self, token: _Token) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "_BearerAuth(<hidden>)"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = "Bearer " + self._token.value
        yield request


@dataclass(frozen=True)
class Response:
    """A decoded response; the repr leaves out the body and headers (tracebacks)."""

    status: int
    body: Any = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)


class SePayEInvoiceClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        credentials: CredentialResolver,
        clock: Clock,
        limiter: ProcessRateLimiter,
        base_urls: Mapping[Environment, str] | None = None,
        http_timeout_s: float = 20.0,
        artifact_timeout_s: float = 90.0,
    ) -> None:
        """``artifact_timeout_s`` bounds a PDF/XML download, which the sandbox served in
        over 20 s; a longer wait there is safe because a download is a GET that carries no
        POST budget and no lease.
        """
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
        self.artifact_timeout_s = artifact_timeout_s
        # A token is bound to the environment that issued it (docs: a sandbox token gets
        # 401 on production), so the cache key carries both.
        self._tokens: dict[tuple[str, Environment], _Token] = {}

    def base_url_for(self, environment: Environment) -> str:
        url = self._base_urls.get(Environment(environment))
        if url is None:
            raise ProviderConfigError(f"no SePay eInvoice URL configured for {environment}")
        return url.rstrip("/")

    def invalidate(self, account: IssuerAccountView) -> None:
        self._tokens.pop(_token_key(account), None)

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
        timeout_s: float | None = None,
    ) -> Response:
        """Send one API call and return a success (or ``accept``-listed) response.

        Any other status is raised as a port error. ``SendAborted`` means no request was
        sent because ``before_send`` returned ``False``. ``timeout_s`` replaces
        ``http_timeout_s`` for this call only.
        """
        url = self.base_url_for(account.environment) + path
        timeout = self._timeout if timeout_s is None else timeout_s
        token, fresh = await self._token(account, creds)
        await self._limiter.acquire()
        if before_send is not None and not await before_send():
            raise SendAborted()
        response = await self._send(method, url, token, json, timeout)
        if response.status == 401:
            self.invalidate(account)
            if method != "GET":
                # Never resend a create: the next call starts with a new token.
                if fresh:
                    raise ProviderAuthFailed("token rejected")
                raise ProviderTransient(retry_after=None)
            token, _ = await self._token(account, creds)
            await self._limiter.acquire()
            response = await self._send(method, url, token, json, timeout)
            if response.status == 401:
                self.invalidate(account)
                raise ProviderAuthFailed("token rejected")
        _log(method, path, response)
        if response.status in OK_STATUSES or response.status in accept:
            return response
        raise map_error(response.status, response.body, response.headers)

    async def _token(
        self, account: IssuerAccountView, creds: ClientCredentials | None
    ) -> tuple[_Token, bool]:
        key = _token_key(account)
        cached = self._tokens.get(key)
        now = self._clock.now()
        if (
            cached is not None
            and cached.expires_at is not None
            and now < cached.expires_at - REFRESH_MARGIN
        ):
            return cached, False
        if creds is None:
            creds = await self._credentials.client_credentials(account.credential_ref)
        method, path = endpoint("token")
        await self._limiter.acquire()
        raw = await self._call(
            method,
            self.base_url_for(account.environment) + path,
            auth=httpx.BasicAuth(creds.client_id, creds.client_secret),
            timeout=self._timeout,
        )
        token = _parse_token(_response(raw), now)
        self._tokens[key] = token
        return token, True

    async def _send(
        self,
        method: str,
        url: str,
        token: _Token,
        body: Mapping[str, object] | None,
        timeout: float,
    ) -> Response:
        raw = await self._call(
            method,
            url,
            json=dict(body) if body is not None else None,
            auth=_BearerAuth(token),
            timeout=timeout,
        )
        return _response(raw)

    async def _call(self, method: str, url: str, **options: Any) -> httpx.Response:
        """One transport call; a transport failure becomes ``ProviderTransient``.

        A refused header (``LocalProtocolError``, or a value that cannot be encoded) is not
        chained: its message quotes the header, which may be the ``Authorization`` value.
        """
        try:
            return await self._http.request(method, url, follow_redirects=False, **options)
        except (httpx.LocalProtocolError, UnicodeEncodeError) as error:
            refused = type(error).__name__
        except httpx.HTTPError as error:
            logger.warning("sepay einvoice %s request failed: %s", method, type(error).__name__)
            raise ProviderTransient(retry_after=None) from error
        logger.warning("sepay einvoice %s request failed: %s", method, refused)
        raise ProviderTransient(retry_after=None)


def _token_key(account: IssuerAccountView) -> tuple[str, Environment]:
    return account.credential_ref, Environment(account.environment)


def _parse_token(response: Response, now: datetime) -> _Token:
    """The token of a ``/v1/token`` response.

    The raw body is read only inside ``_usable_token``, so no frame of a refusal holds it.
    """
    if response.status not in OK_STATUSES:
        logger.warning("sepay einvoice token request failed with HTTP %s", response.status)
        raise map_error(response.status, response.body, response.headers)
    token = _usable_token(response.body, now)
    if token is None:
        raise ProviderAuthFailed("token response without a usable access token")
    return token


def _usable_token(body: Any, now: datetime) -> _Token | None:
    """An RFC 6750 token and its expiry; nothing can raise once the value is read."""
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    ttl = data.get("expires_in")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 0 < ttl <= MAX_TOKEN_TTL_SECONDS:
        ttl = TOKEN_TTL_SECONDS
    expires_at = now + timedelta(seconds=ttl)
    value = data.get("access_token")
    if not isinstance(value, str) or ACCESS_TOKEN.fullmatch(value) is None:
        return None
    return _Token(value, expires_at)


def _log(method: str, path: str, response: Response) -> None:
    """One line per response, without the body or any provider text."""
    level = logging.DEBUG if response.status in OK_STATUSES else logging.WARNING
    body = response.body if isinstance(response.body, Mapping) else {}
    data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
    error = body.get("error") if isinstance(body.get("error"), Mapping) else {}
    message = error.get("message", data.get("message"))
    status = data.get("status")
    logger.log(
        level,
        "sepay einvoice %s %s -> HTTP %s code=%s status=%s message_length=%s",
        method,
        path,
        response.status,
        error_code(body),
        status if isinstance(status, str) else None,
        len(message) if isinstance(message, str) else None,
    )


class _RefusedNumber(ValueError):
    """A JSON number this adapter does not read: ``NaN``, an infinity or a huge exponent."""


def _json_number(text: str) -> Decimal:
    number = bounded_decimal(text)
    if number is None:
        raise _RefusedNumber
    return number


def _json_constant(name: str) -> NoReturn:
    raise _RefusedNumber


def _response(raw: httpx.Response) -> Response:
    """The decoded response; a body that is not JSON is ``None``.

    Numbers are read as ``Decimal``, never ``float``. A body with a refused number is an
    unclear provider answer: ``ProviderTransient``, raised outside the decoder's frames,
    which hold the whole body.
    """
    body: Any = None
    refused = False
    if raw.content:
        try:
            body = json.loads(raw.content, parse_float=_json_number, parse_constant=_json_constant)
        except _RefusedNumber:
            refused = True
        except (ValueError, RecursionError):
            body = None
    if refused:
        raise ProviderTransient(retry_after=None)
    return Response(status=raw.status_code, body=body, headers=dict(raw.headers))
