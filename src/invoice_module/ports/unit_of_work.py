from typing import Any, Protocol


class Repository(Protocol):
    """Marker protocol; method sets are owned by the application phases."""


class InvoiceUnitOfWork(Protocol):
    issuer_accounts: Repository
    drafts: Repository
    receipts: Repository
    documents: Repository
    review_cases: Repository

    async def lock_host_ref(self, tenant_id: str, host_ref_type: str, host_ref_id: str) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def __aenter__(self) -> "InvoiceUnitOfWork": ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class InvoiceUnitOfWorkFactory(Protocol):
    def __call__(self) -> InvoiceUnitOfWork: ...
