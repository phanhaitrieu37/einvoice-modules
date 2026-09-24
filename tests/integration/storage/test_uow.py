from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.support.db import Rows

from invoice_module.adapters.sqlalchemy.tables import InvoiceTables
from invoice_module.adapters.sqlalchemy.uow import SqlAlchemyInvoiceUnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def draft_row(tables: InvoiceTables, **over: object) -> dict[str, object]:
    rows = Rows.__new__(Rows)
    rows.counter = 0
    return rows.draft_row(**over)


async def _present(engine: AsyncEngine, table: object, row_id: uuid.UUID) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(select(table.c.id).where(table.c.id == row_id))  # type: ignore[attr-defined]
        return result.scalar_one_or_none() is not None


async def test_owned_uow_commits(
    engine: AsyncEngine, inv_tables: InvoiceTables, sessions: async_sessionmaker[AsyncSession]
) -> None:
    row = draft_row(inv_tables)
    async with SqlAlchemyInvoiceUnitOfWork(sessions, inv_tables) as uow:
        await uow.session.execute(inv_tables.invoice_drafts.insert().values(**row))
        await uow.commit()
    assert await _present(engine, inv_tables.invoice_drafts, row["id"])  # type: ignore[arg-type]


async def test_owned_uow_rolls_back_without_commit(
    engine: AsyncEngine, inv_tables: InvoiceTables, sessions: async_sessionmaker[AsyncSession]
) -> None:
    row = draft_row(inv_tables)
    async with SqlAlchemyInvoiceUnitOfWork(sessions, inv_tables) as uow:
        await uow.session.execute(inv_tables.invoice_drafts.insert().values(**row))
    assert not await _present(engine, inv_tables.invoice_drafts, row["id"])  # type: ignore[arg-type]


async def test_joined_uow_host_commit_persists(
    engine: AsyncEngine, inv_tables: InvoiceTables
) -> None:
    row = draft_row(inv_tables)
    async with (
        AsyncSession(engine) as host,
        host.begin(),
        SqlAlchemyInvoiceUnitOfWork.joined(host, inv_tables) as uow,
    ):
        await uow.session.execute(inv_tables.invoice_drafts.insert().values(**row))
        await uow.commit()
    assert await _present(engine, inv_tables.invoice_drafts, row["id"])  # type: ignore[arg-type]


async def test_joined_uow_host_rollback_discards(
    engine: AsyncEngine, inv_tables: InvoiceTables
) -> None:
    row = draft_row(inv_tables)
    async with AsyncSession(engine) as host:
        await host.begin()
        async with SqlAlchemyInvoiceUnitOfWork.joined(host, inv_tables) as uow:
            connection = await uow.session.connection()
            await connection.execute(inv_tables.invoice_drafts.insert().values(**row))
            await uow.commit()
        await host.rollback()
    assert not await _present(engine, inv_tables.invoice_drafts, row["id"])  # type: ignore[arg-type]


async def test_joined_uow_rollback_discards_before_host_commit(
    engine: AsyncEngine, inv_tables: InvoiceTables
) -> None:
    row = draft_row(inv_tables)
    async with AsyncSession(engine) as host:
        await host.begin()
        async with SqlAlchemyInvoiceUnitOfWork.joined(host, inv_tables) as uow:
            await uow.session.execute(inv_tables.invoice_drafts.insert().values(**row))
            await uow.rollback()
        await host.commit()
    assert not await _present(engine, inv_tables.invoice_drafts, row["id"])  # type: ignore[arg-type]


async def test_lock_host_ref_serializes_two_sessions(
    sessions: async_sessionmaker[AsyncSession], inv_tables: InvoiceTables
) -> None:
    acquired_a, attempted_b, acquired_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def session_a() -> None:
        async with sessions() as session:
            await session.begin()
            async with SqlAlchemyInvoiceUnitOfWork.joined(session, inv_tables) as uow:
                await uow.lock_host_ref("tenant-test", "order", "same")
                acquired_a.set()
                await attempted_b.wait()
                await asyncio.sleep(0.1)
                assert not acquired_b.is_set()
            await session.commit()
            order.append("a_commit")

    async def session_b() -> None:
        await acquired_a.wait()
        async with sessions() as session:
            await session.begin()
            async with SqlAlchemyInvoiceUnitOfWork.joined(session, inv_tables) as uow:
                attempted_b.set()
                await uow.lock_host_ref("tenant-test", "order", "same")
            await session.commit()
            order.append("b_acquired")
            acquired_b.set()

    await asyncio.wait_for(asyncio.gather(session_a(), session_b()), timeout=5)
    assert order == ["a_commit", "b_acquired"]


async def test_joined_rollback_keeps_lock_until_host_commit(
    sessions: async_sessionmaker[AsyncSession], inv_tables: InvoiceTables
) -> None:
    locked, attempted_b, acquired_b = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def host_session() -> None:
        async with sessions() as host:
            await host.begin()
            async with SqlAlchemyInvoiceUnitOfWork.joined(host, inv_tables) as uow:
                await uow.lock_host_ref("tenant-test", "order", "rollback")
                await uow.rollback()
                locked.set()
                await attempted_b.wait()
                await asyncio.sleep(0.1)
                assert not acquired_b.is_set()
            await host.commit()

    async def contender() -> None:
        await locked.wait()
        async with sessions() as session:
            await session.begin()
            async with SqlAlchemyInvoiceUnitOfWork.joined(session, inv_tables) as uow:
                attempted_b.set()
                await uow.lock_host_ref("tenant-test", "order", "rollback")
            await session.commit()
            acquired_b.set()

    await asyncio.wait_for(asyncio.gather(host_session(), contender()), timeout=5)
    assert acquired_b.is_set()


async def test_joined_write_then_lock_fails_without_losing_row(
    engine: AsyncEngine, inv_tables: InvoiceTables
) -> None:
    row = draft_row(inv_tables)
    async with AsyncSession(engine) as host:
        await host.begin()
        async with SqlAlchemyInvoiceUnitOfWork.joined(host, inv_tables) as uow:
            connection = await uow.session.connection()
            await connection.execute(inv_tables.invoice_drafts.insert().values(**row))
            with pytest.raises(RuntimeError, match="lock_host_ref must precede joined work"):
                await uow.lock_host_ref("tenant-test", "order", "written-first")
            await uow.commit()
        await host.commit()
    assert await _present(engine, inv_tables.invoice_drafts, row["id"])  # type: ignore[arg-type]


async def test_joined_lock_write_lock_again_keeps_row(
    engine: AsyncEngine, inv_tables: InvoiceTables
) -> None:
    row = draft_row(inv_tables)
    async with AsyncSession(engine) as host:
        await host.begin()
        async with SqlAlchemyInvoiceUnitOfWork.joined(host, inv_tables) as uow:
            await uow.lock_host_ref("tenant-test", "order", "repeat")
            await uow.session.execute(inv_tables.invoice_drafts.insert().values(**row))
            await uow.lock_host_ref("tenant-test", "order", "repeat")
            await uow.commit()
        await host.commit()
    assert await _present(engine, inv_tables.invoice_drafts, row["id"])  # type: ignore[arg-type]
