from types import TracebackType
from typing import Self

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRepository
from invoice_module.adapters.sqlalchemy.repositories.drafts import DraftRepository
from invoice_module.adapters.sqlalchemy.repositories.issuer_accounts import IssuerAccountRepository
from invoice_module.adapters.sqlalchemy.repositories.receipts import ReceiptRepository
from invoice_module.adapters.sqlalchemy.repositories.review_cases import ReviewCaseRepository
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
        self._savepoint_touched = False
        self._joined_committed_work = False
        self._locked_host_refs: set[str] = set()
        self._tracking_connection = None
        self.issuer_accounts: IssuerAccountRepository
        self.drafts: DraftRepository
        self.receipts: ReceiptRepository
        self.documents: DocumentRepository
        self.review_cases: ReviewCaseRepository

    @classmethod
    def joined(cls, session: AsyncSession, tables: InvoiceTables) -> Self:
        uow = cls.__new__(cls)
        uow._session_factory = None
        uow._tables = tables
        uow._session = None
        uow._host_session = session
        uow._savepoint = None
        uow._savepoint_touched = False
        uow._joined_committed_work = False
        uow._locked_host_refs = set()
        uow._tracking_connection = None
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
            await self._track_savepoint_work()
        else:
            assert self._session_factory is not None
            self._session = self._session_factory()
        self.issuer_accounts = IssuerAccountRepository(self.session, self._tables)
        self.drafts = DraftRepository(self.session, self._tables)
        self.receipts = ReceiptRepository(self.session, self._tables)
        self.documents = DocumentRepository(self.session, self._tables)
        self.review_cases = ReviewCaseRepository(self.session, self._tables)
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
            try:
                if not self.is_joined and self._session is not None:
                    await self._session.close()
            finally:
                if not self.is_joined:
                    self._locked_host_refs.clear()
                self._session = None

    async def commit(self) -> None:
        if self.is_joined:
            had_work = bool(self.session.new or self.session.dirty or self.session.deleted)
            await self.session.flush()
            self._joined_committed_work |= had_work or self._savepoint_touched
            self._stop_tracking_savepoint_work()
            if self._savepoint is not None and self._savepoint.is_active:
                await self._savepoint.commit()
            self._savepoint = None
        else:
            await self.session.commit()
            self._locked_host_refs.clear()

    async def rollback(self) -> None:
        if self._session is None:
            return
        if self.is_joined:
            self._stop_tracking_savepoint_work()
            if self._savepoint is not None and self._savepoint.is_active:
                await self._savepoint.rollback()
            self._savepoint = None
            self._savepoint_touched = False
        else:
            await self.session.rollback()
            self._locked_host_refs.clear()

    async def lock_host_ref(self, tenant_id: str, host_ref_type: str, host_ref_id: str) -> None:
        key = f"{tenant_id}:{host_ref_type}:{host_ref_id}"
        if key in self._locked_host_refs:
            return
        if self.is_joined:
            if self._joined_committed_work:
                raise RuntimeError("lock_host_ref must precede joined work")
            if self.session.new or self.session.dirty or self.session.deleted:
                raise RuntimeError("lock_host_ref must precede joined work")
            if self._savepoint is not None and self._savepoint.is_active:
                if self._savepoint_touched:
                    raise RuntimeError("lock_host_ref must precede joined work")
                self._stop_tracking_savepoint_work()
                await self._savepoint.commit()
                self._savepoint = None
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key}
        )
        if self.is_joined:
            self._savepoint = await self.session.begin_nested()
            self._savepoint_touched = False
            await self._track_savepoint_work()
        self._locked_host_refs.add(key)

    async def _track_savepoint_work(self) -> None:
        if not self.is_joined or self._tracking_connection is not None:
            return
        connection = await self.session.connection()
        self._tracking_connection = connection.sync_connection
        event.listen(
            self._tracking_connection,
            "before_cursor_execute",
            self._mark_savepoint_touched,
        )

    def _stop_tracking_savepoint_work(self) -> None:
        if self._tracking_connection is None:
            return
        event.remove(
            self._tracking_connection,
            "before_cursor_execute",
            self._mark_savepoint_touched,
        )
        self._tracking_connection = None

    def _mark_savepoint_touched(self, *args: object, **kwargs: object) -> None:
        self._savepoint_touched = True


class SqlAlchemyInvoiceUnitOfWorkFactory:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], tables: InvoiceTables
    ) -> None:
        self._session_factory = session_factory
        self._tables = tables

    def __call__(self) -> SqlAlchemyInvoiceUnitOfWork:
        return SqlAlchemyInvoiceUnitOfWork(self._session_factory, self._tables)
