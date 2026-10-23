"""Resolve ``env:NAME`` credential references from environment variables.

``env:NAME`` reads ``NAME_CLIENT_ID`` and ``NAME_CLIENT_SECRET``. An unset, empty or
whitespace-only variable is a configuration error whose message names the variable, never
a value. A reference with any other scheme is rejected with ``ValueError``.
"""

import os
from collections.abc import Mapping

from invoice_module.ports.provider import ClientCredentials, ProviderConfigError

SCHEME = "env:"
CLIENT_ID_SUFFIX = "_CLIENT_ID"
CLIENT_SECRET_SUFFIX = "_CLIENT_SECRET"


class EnvCredentialResolver:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        """``environ`` defaults to :data:`os.environ`, read on every call."""
        self._environ = os.environ if environ is None else environ

    async def client_credentials(self, ref: str) -> ClientCredentials:
        name = _variable(ref)
        return ClientCredentials(
            client_id=self._value(name + CLIENT_ID_SUFFIX),
            client_secret=self._value(name + CLIENT_SECRET_SUFFIX),
        )

    def _value(self, name: str) -> str:
        value = self._environ.get(name)
        if value is None or not value.strip():
            raise ProviderConfigError(f"credential variable {name} is not set")
        return value


def _variable(ref: str) -> str:
    if not ref.startswith(SCHEME) or not ref[len(SCHEME) :].strip():
        raise ValueError("credential reference must look like 'env:NAME'")
    return ref[len(SCHEME) :].strip()
