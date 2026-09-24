"""HTTP client locked to the SePay eInvoice sandbox."""

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
        self._http = httpx.Client(base_url=SANDBOX_BASE_URL, transport=transport, timeout=30.0)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_request_at: float | None = None

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
        authenticated: bool = True,
    ) -> tuple[int, Any, dict[str, str | None]]:
        headers: dict[str, str] = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._get_token()}"
        request = self._http.build_request(method, path, json=payload, headers=headers)
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
        return response.status_code, body, {"content-type": response.headers.get("content-type")}

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - 30:
            return self._token
        status, body, _ = self.request_json(
            str(CANDIDATES["token"]["method"]),
            str(CANDIDATES["token"]["path"]),
            payload={
                "grant_type": "client_credentials",
                "client_id": self._env.get("SEPAY_EINVOICE_SANDBOX_CLIENT_ID", ""),
                "client_secret": self._env.get("SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET", ""),
            },
            authenticated=False,
        )
        if status >= 400 or not isinstance(body, Mapping):
            raise RuntimeError(f"token request failed with HTTP {status}")
        token = body.get("access_token") or body.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("token response did not contain an access token")
        expires_in = body.get("expires_in", 3600)
        self._token = token
        self._token_expires_at = time.monotonic() + (
            expires_in if isinstance(expires_in, int) else 3600
        )
        return token

    def _pace(self) -> None:
        if self._last_request_at is not None:
            remaining = 0.5 - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
