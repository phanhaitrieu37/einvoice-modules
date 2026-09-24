from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invoice_module.adapters.sqlalchemy.repositories.base import SqlAlchemyRepository
from invoice_module.adapters.sqlalchemy.tables import InvoiceTables


class SqlAlchemyInvoiceUnitOfWork:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], tables: InvoiceTables
    ) -> None:
        self._session_factory: async_sessionmaker[AsyncSession] | None = session_factory
        self._tables = tables
        self._session: AsyncSession | None = None
        self._host_session: AsyncSession | None = None
        self._savepoint = None
        self.issuer_accounts: SqlAlchemyRepository
        self.drafts: SqlAlchemyRepository
        self.receipts: SqlAlchemyRepository
        self.documents: SqlAlchemyRepository
        self.review_cases: SqlAlchemyRepository

    @classmethod
    def joined(cls, session: AsyncSession, tables: InvoiceTables) -> Self:
        uow = cls.__new__(cls)
        uow._session_factory = None
        uow._tables = tables
        uow._session = None
        uow._host_session = session
        uow._savepoint = None
        return uow

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session

    @property
    def is_joined(self) -> bool:
        return self._host_session is not None

    async def __aenter__(self) -> Self:
        if self._host_session is not None:
            self._session = self._host_session
            self._savepoint = await self._session.begin_nested()
        else:
            assert self._session_factory is not None
            self._session = self._session_factory()
        for name in ("issuer_accounts", "drafts", "receipts", "documents", "review_cases"):
            setattr(self, name, SqlAlchemyRepository(self.session, self._tables))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.rollback()
        finally:
            if not self.is_joined and self._session is not None:
                await self._session.close()
            self._session = None

    async def commit(self) -> None:
        if self.is_joined:
            await self.session.flush()
            if self._savepoint is not None and self._savepoint.is_active:
                await self._savepoint.commit()
            self._savepoint = None
        else:
            await self.session.commit()

    async def rollback(self) -> None:
        if self._session is None:
            return
        if self.is_joined:
            if self._savepoint is not None and self._savepoint.is_active:
                await self._savepoint.rollback()
            self._savepoint = None
        else:
            await self.session.rollback()

    async def lock_host_ref(self, tenant_id: str, host_ref_type: str, host_ref_id: str) -> None:
        key = f"{tenant_id}:{host_ref_type}:{host_ref_id}"
        if self.is_joined and self._savepoint is not None and self._savepoint.is_active:
            await self._savepoint.rollback()
            self._savepoint = None
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key}
        )
        if self.is_joined:
            self._savepoint = await self.session.begin_nested()


class SqlAlchemyInvoiceUnitOfWorkFactory:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], tables: InvoiceTables
    ) -> None:
        self._session_factory = session_factory
        self._tables = tables

    def __call__(self) -> SqlAlchemyInvoiceUnitOfWork:
        return SqlAlchemyInvoiceUnitOfWork(self._session_factory, self._tables)
