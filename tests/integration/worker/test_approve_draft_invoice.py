import asyncio
import hashlib
import logging
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from tests.integration.worker.world import TENANT, vn

from invoice_module.adapters.sqlalchemy.repositories.documents import approval_valid
from invoice_module.application.approve_draft_invoice import ApprovalNotAllowed
from invoice_module.application.submit_draft import SubmitDraftCommand
from invoice_module.domain.document import Buyer, InvoiceDocument
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.money import LineInput

pytestmark = pytest.mark.integration

MANUAL = DeliveryMode.MANUAL
BUYER = Buyer(name="Synthetic Buyer Name", address="Synthetic street 1")


async def awaiting(world, **settle):
    settled = await world.settle(mode=MANUAL, **settle)
    await world.issue()
    assert (await world.doc(settled.document_id)).status == "awaiting_approval"
    return settled


def snapshot_hash(document):
    canonical = InvoiceDocument.from_json(document).canonical_json()
    return hashlib.sha256(canonical.encode()).hexdigest()


async def test_approve_moves_awaiting_to_approved_with_actor(world, caplog):
    host_ref = f"order-{uuid4()}"
    intent = uuid4()
    await world.module.submit_draft.execute(
        SubmitDraftCommand(
            TENANT,
            world.merchant_id,
            "order",
            host_ref,
            intent,
            (LineInput("Synthetic service", "each", Decimal("1"), 100, 0, item_code="SYN-ITEM-1"),),
            100,
            buyer=BUYER,
            delivery_mode=MANUAL,
        )
    )
    settled = await world.settle(mode=MANUAL, draft=False, host_ref_id=host_ref)
    await world.set_columns(settled.document_id, intent_id=intent)
    await world.module.escalate_overdue.run(world.clock.now())
    await world.issue()
    before = await world.doc(settled.document_id)
    assert before.status == "awaiting_approval"
    world.advance(timedelta(minutes=30))
    with caplog.at_level(logging.INFO):
        result = await world.module.approve_draft_invoice.execute(
            settled.document_id, " accountant-a ", note="Checked against the delivery note"
        )
    doc = await world.doc(settled.document_id)
    assert (result.status, result.already_approved) == ("approved", False)
    assert (doc.status, doc.approved_by, doc.approved_at, doc.attempts) == (
        "approved",
        "accountant-a",
        world.clock.now(),
        0,
    )
    assert doc.approved_snapshot_sha256 == snapshot_hash(doc.document)
    assert doc.approved_reference_code == doc.reference_code == settled.reference
    assert (doc.next_attempt_at, doc.lease_generation) == (
        world.clock.now(),
        before.lease_generation + 1,
    )
    assert world.metrics.count("invoice_draft_approved_total") == 1
    [record] = [r for r in caplog.records if r.message == "invoice_draft_approved"]
    assert record.actor == "accountant-a"
    assert all("Synthetic Buyer Name" not in str(value) for value in record.__dict__.values())


@pytest.mark.parametrize("actor", ["", " ", "\t", "\n", " \t\n "])
async def test_approve_requires_nonblank_actor(world, actor):
    settled = await awaiting(world)
    before = await world.doc(settled.document_id)
    with pytest.raises(ValueError):
        await world.module.approve_draft_invoice.execute(settled.document_id, actor)
    assert await world.doc(settled.document_id) == before


@pytest.mark.parametrize("state", ["queued", "needs_draft", "blocked", "failed"])
async def test_approve_refuses_other_statuses(world, state):
    if state == "queued":
        settled = await world.settle(mode=MANUAL)
    elif state == "needs_draft":
        settled = await world.settle(mode=MANUAL, draft=False)
    else:
        settled = await awaiting(world)
        if state == "blocked":
            await world.module.hold_draft_invoice.execute(settled.document_id, "a", "wrong")
        else:
            await world.set_columns(settled.document_id, status="failed")
    before = await world.doc(settled.document_id)
    with pytest.raises(ApprovalNotAllowed) as caught:
        await world.module.approve_draft_invoice.execute(settled.document_id, "accountant-a")
    assert caught.value.reason == state
    assert await world.doc(settled.document_id) == before


async def test_approve_twice_is_idempotent(world):
    settled = await awaiting(world)
    await world.approve(settled)
    first = await world.doc(settled.document_id)
    world.advance(timedelta(minutes=5))
    again = await world.module.approve_draft_invoice.execute(settled.document_id, "accountant-b")
    assert (again.already_approved, again.status) == (True, "approved")
    assert await world.doc(settled.document_id) == first


async def test_concurrent_approvals_single_transition(world):
    settled = await awaiting(world)
    before = await world.doc(settled.document_id)
    first, second = world.build(), world.build()
    table = world.tables.documents
    async with world.engine.connect() as holder:
        await holder.execute(
            sa.select(table.c.id).where(table.c.id == settled.document_id).with_for_update()
        )
        tasks = [
            asyncio.create_task(module.approve_draft_invoice.execute(settled.document_id, actor))
            for module, actor in ((first, "accountant-a"), (second, "accountant-b"))
        ]
        await asyncio.sleep(0.3)
        assert not any(task.done() for task in tasks)
        await holder.rollback()
    results = await asyncio.gather(*tasks)
    doc = await world.doc(settled.document_id)
    assert sorted(result.already_approved for result in results) == [False, True]
    assert doc.lease_generation == before.lease_generation + 1
    assert doc.approved_by in ("accountant-a", "accountant-b")


async def test_worker_issues_only_after_approval(world):
    settled = await world.settle(mode=MANUAL)
    await world.drive(rounds=6)
    doc = await world.doc(settled.document_id)
    assert doc.status == "awaiting_approval"
    assert world.provider.calls == [] and sum(world.provider.effective_creates.values()) == 0
    world.clock.set(vn(2026, 10, 21, 16))
    await world.approve(settled)
    await world.drive(rounds=3, step=timedelta(seconds=5))
    doc = await world.doc(settled.document_id)
    [post] = world.provider.posts()
    assert world.provider.ops()[:3] == ["find_by_reference", "create", "check_create"]
    assert post.payload.get("is_draft", False) is False
    assert (doc.status, doc.approved_by, doc.approved_at) == (
        "issued",
        "accountant-a",
        vn(2026, 10, 21, 16),
    )
    assert (
        doc.issued_date
        == date(2026, 10, 21)
        == world.provider.invoices[post.reference_code].issued_date
    )
    assert world.provider.effective_creates[settled.reference] == 1


async def test_approval_across_midnight_records_provider_issue_date(world):
    world.clock.set(vn(2026, 10, 21, 22))
    settled = await awaiting(world)
    world.clock.set(vn(2026, 10, 22, 8))
    await world.approve(settled)
    await world.drive(rounds=3, step=timedelta(seconds=5))
    doc = await world.doc(settled.document_id)
    [post] = world.provider.posts()
    assert (doc.status, doc.issued_date) == ("issued", date(2026, 10, 22))
    assert post.payload["issued_date"][:10] == "2026-10-22"
    assert doc.approved_at == vn(2026, 10, 22, 8)


async def test_list_pending_approvals_filters_and_orders(world):
    world.clock.set(vn(2026, 10, 22, 10))
    later = await awaiting(world)
    world.clock.set(vn(2026, 10, 21, 10))
    earlier = await awaiting(world)
    auto = await world.settle()
    await world.issue()
    world.clock.set(vn(2026, 10, 22, 12))
    views = await world.module.list_pending_approvals.execute(tenant_id=TENANT)
    assert [view.document_id for view in views] == [earlier.document_id, later.document_id]
    first = views[0]
    assert (first.settlement_id, first.host_ref_id, first.total_vnd) == (
        earlier.settlement_id,
        earlier.host_ref_id,
        100,
    )
    assert first.buyer_name == "Synthetic fallback buyer"
    assert first.age == vn(2026, 10, 22, 12) - vn(2026, 10, 21, 10)
    overdue = await world.module.list_pending_approvals.execute(overdue_only=True)
    assert [view.document_id for view in overdue] == [earlier.document_id]
    assert auto.document_id not in [view.document_id for view in views]
    assert await world.module.list_pending_approvals.execute(tenant_id="other") == []


async def test_approval_binds_snapshot_hash_and_reference(world):
    settled = await awaiting(world)
    await world.approve(settled)
    doc = await world.doc(settled.document_id)
    assert doc.approved_snapshot_sha256 == snapshot_hash(doc.document)
    async with world.crash() as uow:
        row = await uow.documents.get(settled.document_id)
    assert approval_valid(row)
    changed = dict(doc.document)
    changed["notes"] = "edited"
    await world.set_columns(settled.document_id, document=changed)
    async with world.crash() as uow:
        assert not approval_valid(await uow.documents.get(settled.document_id))
    with pytest.raises(IntegrityError, match="approved_reference_ck"):
        await world.set_columns(settled.document_id, approved_reference_code=str(uuid4()))


async def test_approve_with_mismatched_expected_hash_refused(world):
    settled = await awaiting(world)
    before = await world.doc(settled.document_id)
    with pytest.raises(ApprovalNotAllowed) as caught:
        await world.module.approve_draft_invoice.execute(
            settled.document_id, "accountant-a", expected_snapshot_sha256="0" * 64
        )
    assert caught.value.reason == "snapshot_changed"
    assert await world.doc(settled.document_id) == before
    shown = await world.module.get_draft_for_approval.execute(settled.document_id)
    result = await world.module.approve_draft_invoice.execute(
        settled.document_id, "accountant-a", expected_snapshot_sha256=shown.snapshot_sha256
    )
    assert result.status == "approved"


async def test_get_draft_for_approval_returns_full_snapshot(world):
    settled = await awaiting(world, amount=110, draft_amount=110)
    view = await world.module.get_draft_for_approval.execute(settled.document_id)
    doc = await world.doc(settled.document_id)
    assert (view.status, view.delivery_mode, view.reference_code, view.total_vnd) == (
        "awaiting_approval",
        "manual",
        settled.reference,
        110,
    )
    assert view.buyer == {"name": "Synthetic fallback buyer", "address": ""}
    [line] = view.lines
    assert {
        key: line[key] for key in ("name", "unit", "quantity", "unit_price_vnd", "tax_rate")
    } == {
        "name": "Synthetic service",
        "unit": "each",
        "quantity": "1",
        "unit_price_vnd": 110,
        "tax_rate": 0,
    }
    assert (line["tax_vnd"], line["line_total_vnd"]) == (0, 110)
    assert view.snapshot_sha256 == snapshot_hash(doc.document)
    assert await world.module.get_draft_for_approval.execute(uuid4()) is None
