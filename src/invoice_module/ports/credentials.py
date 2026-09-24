from typing import Protocol

from invoice_module.ports.provider import ClientCredentials


class CredentialResolver(Protocol):
    async def client_credentials(self, ref: str) -> ClientCredentials: ...
