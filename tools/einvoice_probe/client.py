"""Sandbox API client with controlled HTTPS artifact fetching."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx

from .candidates import CANDIDATES

SANDBOX_BASE_URL = "https://einvoice-api-sandbox.sepay.vn"


class ProbeClient:
    """Small synchronous client for offline-testable sandbox probe scenarios."""

    def __init__(
        self, env: Mapping[str, str], transport: httpx.BaseTransport | None = None
    ) -> None:
        if env.get("SEPAY_EINVOICE_ENVIRONMENT") != "test":
            raise ValueError("SEPAY_EINVOICE_ENVIRONMENT must be test")
        self._env = dict(env)
        self._pace_enabled = transport is None
        self._http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=transport, timeout=30.0)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_request_at: float | None = None
        self.token_response: dict[str, Any] | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ProbeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        authenticated: bool = True,
    ) -> tuple[int, Any, dict[str, str | None]]:
        headers: dict[str, str] = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._get_token()}"
        request = self._http.build_request(
            method, path, json=payload, params=params, headers=headers
        )
        if request.url.scheme != "https" or request.url.host != "einvoice-api-sandbox.sepay.vn":
            raise ValueError("requests must target the SePay eInvoice sandbox")
        self._pace()
        response = self._http.send(request)
        self._last_request_at = time.monotonic()
        try:
            body: Any = response.json()
        except ValueError:
            body = {
                "content_type": response.headers.get("content-type"),
                "size": len(response.content),
            }
        return (
            response.status_code,
            body,
            {
                "content-type": response.headers.get("content-type"),
                "content-length": response.headers.get("content-length"),
            },
        )

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - 30:
            return self._token
        client_id = self._env.get("SEPAY_EINVOICE_SANDBOX_CLIENT_ID", "")
        client_secret = self._env.get("SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET", "")
        self._pace()
        response = self._http.post(
            str(CANDIDATES["token"]["path"]),
            auth=httpx.BasicAuth(client_id, client_secret),
        )
        self._last_request_at = time.monotonic()
        try:
            body = response.json()
        except ValueError as error:
            raise RuntimeError(f"token request failed with HTTP {response.status_code}") from error
        self.token_response = {"status": response.status_code, "body": body}
        if response.status_code >= 400 or not isinstance(body, Mapping):
            raise RuntimeError(f"token request failed with HTTP {response.status_code}")
        data = body.get("data", body)
        token = data.get("access_token") if isinstance(data, Mapping) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("token response did not contain an access token")
        expires_in = data.get("expires_in", 86400)
        self._token = token
        self._token_expires_at = time.monotonic() + (
            expires_in if isinstance(expires_in, int) else 86400
        )
        return token

    def _pace(self) -> None:
        if not self._pace_enabled:
            return
        if self._last_request_at is not None:
            remaining = 0.5 - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
