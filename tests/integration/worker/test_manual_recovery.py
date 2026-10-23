"""Manual mode: the draft stays in the module until approval; then one create issues it.

Each scenario below is one row of the manual recovery matrix; ``test_matrix_manual_rows``
runs them one by one and ``test_no_path_uses_removed_edges`` runs them all while recording
every status edge the code asks for.
"""

import logging
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.fakes.fake_einvoice_provider import (
    AUTH_FAILED,
    CONFLICT,
    LOST_RESPONSE,
    QUOTA,
    TRANSIENT,
    FakeInvoice,
    failed_invoice,
    rejected,
)
from tests.integration.worker.world import vn

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRepository
from invoice_module.application import issue_queued as issue_module
from invoice_module.application.hold_draft_invoice import HoldNotAllowed
from invoice_module.domain.enums import DeliveryMode
from invoice_module.domain.errors import ResolutionNotAllowed
from invoice_module.ports.provider import ProviderInvoice, ProviderProgress

pytestmark = pytest.mark.integration

MANUAL = DeliveryMode.MANUAL
REMOVED_EDGES = {
    ("queued", "approved"),
    ("pending_provider", "awaiting_approval"),
    ("pending_provider", "approved"),
    ("awaiting_approval", "failed"),
    ("blocked", "failed"),
    ("failed", "awaiting_approval"),
}


def issued(post) -> date:
    return date.fromisoformat(str(post.payload["issued_date"])[:10])


async def awaiting(world):
    settled = await world.settle(mode=MANUAL)
    await world.issue()
    return settled


async def approved(world):
    settled = await awaiting(world)
    await world.approve(settled)
    return settled


async def status(world, settled):
    return (await world.doc(settled.document_id)).status


async def queued_unapproved_waits_offline(world):
    settled = await world.settle(mode=MANUAL)
    calls = len(world.provider.calls)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.request is not None) == ("awaiting_approval", True)
    assert world.provider.calls[calls:] == []


async def awaiting_approve_hold_or_alert(world):
    first = await awaiting(world)
    await world.approve(first)
    assert await status(world, first) == "approved"
    second = await awaiting(world)
    await world.module.hold_draft_invoice.execute(second.document_id, "accountant-a", "Wrong")
    assert await status(world, second) == "blocked"
    third = await awaiting(world)
    report = await world.module.escalate_overdue.run(world.clock.now() + timedelta(days=7))
    assert third.document_id in report.approvals_overdue
    assert await status(world, third) == "awaiting_approval"


async def changed_content_drops_approval(world):
    settled = await approved(world)
    doc = await world.doc(settled.document_id)
    changed = dict(doc.document) | {"notes": "changed"}
    await world.set_columns(settled.document_id, document=changed)
    calls = len(world.provider.calls)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at, doc.request["notes"]) == (
        "awaiting_approval",
        None,
        "changed",
    )
    assert world.provider.calls[calls:] == []


async def approved_looks_up_then_sends_once(world):
    settled = await awaiting(world)
    world.advance(timedelta(days=1))
    await world.approve(settled)
    await world.issue()
    [post] = world.provider.posts(settled.reference)
    assert issued(post) == (world.clock.now() + timedelta(hours=7)).date()
    assert world.provider.ops()[-2:] == ["find_by_reference", "create"]


async def approved_lookup_outcomes(world):
    expected = {
        "success": ("issued", None),
        "pending": ("pending_provider", None),
        "failed": ("failed", "PROVIDER_REJECTED"),
        "draft": ("failed", "DOCUMENT_EXISTED_UNRESOLVED"),
    }
    for state, (final, reason) in expected.items():
        settled = await approved(world)
        found = {
            "success": ProviderInvoice(
                settled.reference, "success", invoice_number="9", issued_date=date(2026, 10, 21)
            ),
            "pending": ProviderInvoice(settled.reference, "pending"),
            "failed": failed_invoice(settled.reference, "Synthetic"),
            "draft": ProviderInvoice(settled.reference, "draft"),
        }[state]
        world.provider.script_lookup(settled.reference, found)
        await world.issue()
        assert await status(world, settled) == final, state
        assert world.provider.posts(settled.reference) == []
        if reason:
            assert await world.open_reasons(settled.document_id) == [reason]


async def approved_create_outcomes(world):
    expected = [
        (None, "pending_provider", []),
        (CONFLICT, "failed", ["DOCUMENT_EXISTED_UNRESOLVED"]),
        (TRANSIENT, "approved", []),
        (QUOTA, "approved", ["QUOTA_EXHAUSTED"]),
        (rejected("validation_error"), "failed", ["PROVIDER_REJECTED"]),
        (rejected("issued_date_invalid"), "pending_provider", []),
        (AUTH_FAILED, "failed", ["AUTH_FAILED"]),
    ]
    for outcome, final, reasons in expected:
        settled = await approved(world)
        if outcome is not None:
            world.provider.script_create(settled.reference, outcome)
        await world.issue()
        assert await status(world, settled) == final, outcome
        assert await world.open_reasons(settled.document_id) == reasons, outcome
    aborted = await approved(world)
    world.provider.delay_before_send(aborted.reference, timedelta(seconds=45))
    await world.issue()
    assert await status(world, aborted) == "approved"
    calls = len(world.provider.calls)
    await world.issue()
    assert [
        c.op for c in world.provider.calls[calls:] if c.reference_code == aborted.reference
    ] == ["find_by_reference", "create"]


async def pending_check_outcomes(world):
    for progress, final in (
        (None, "issued"),
        (ProviderProgress(state="failed", provider_message="Synthetic"), "failed"),
        (ProviderProgress(state="pending"), "pending_provider"),
        (ProviderProgress(state="draft"), "failed"),
    ):
        settled = await approved(world)
        await world.issue()
        if progress is not None:
            world.provider.script_check(settled.reference, progress)
        world.advance(timedelta(seconds=3))
        await world.poll()
        assert await status(world, settled) == final
    timeout = await approved(world)
    world.provider.new_invoice(timeout.reference, pending_polls=10_000)
    await world.issue()
    report = await world.module.escalate_overdue.run(world.clock.now() + timedelta(hours=4))
    assert timeout.document_id in report.timed_out


async def held_draft_resolutions(world):
    for resolution, final in (
        ("retry", "awaiting_approval"),
        ("attach_draft", "queued"),
        ("waive", "blocked"),
    ):
        settled = await awaiting(world)
        await world.module.hold_draft_invoice.execute(settled.document_id, "accountant-a", "Wrong")
        kwargs = {}
        if resolution == "attach_draft":
            kwargs["draft"] = world.draft_command(
                settled.host_ref_id, settled.intent_id, mode=MANUAL, name="Fixed"
            )
        note = "Not invoiced" if resolution == "waive" else None
        await world.resolve(settled.document_id, resolution, note=note, **kwargs)
        assert await status(world, settled) == final
        if resolution == "attach_draft":
            await world.issue()
            assert await status(world, settled) == "awaiting_approval"


async def failed_retry_outcomes(world):
    async def failed_manual():
        settled = await approved(world)
        world.provider.script_create(settled.reference, rejected("validation_error"))
        await world.issue()
        return settled

    settled = await failed_manual()
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 100))
    await world.resolve(settled.document_id, "retry")
    assert await status(world, settled) == "issued"
    settled = await failed_manual()
    world.provider.seed_invoice(FakeInvoice(settled.reference, date(2026, 10, 21), 100))
    await world.set_columns(settled.document_id, approved_snapshot_sha256="0" * 64)
    with pytest.raises(ResolutionNotAllowed, match="issued_without_approval"):
        await world.resolve(settled.document_id, "retry")
    settled = await failed_manual()
    world.provider.seed_invoice(
        FakeInvoice(settled.reference, date(2026, 10, 21), 100, pending_polls=2)
    )
    await world.resolve(settled.document_id, "retry")
    assert await status(world, settled) == "pending_provider"
    settled = await failed_manual()
    await world.resolve(settled.document_id, "retry")
    assert await status(world, settled) == "approved"
    settled = await failed_manual()
    await world.set_columns(settled.document_id, approved_snapshot_sha256="0" * 64)
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at) == ("queued", None)


async def failed_marked_external(world):
    settled = await approved(world)
    world.provider.script_create(settled.reference, CONFLICT)
    await world.issue()
    await world.set_columns(
        settled.document_id,
        approved_at=None,
        approved_by=None,
        approved_snapshot_sha256=None,
        approved_reference_code=None,
    )
    await world.resolve(
        settled.document_id,
        "mark_external",
        actor="operator-m",
        invoice_number="00000100",
        issued_date=date(2026, 10, 21),
    )
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_by, doc.approved_reference_code, doc.issued_date) == (
        "issued",
        "operator-m",
        settled.reference,
        date(2026, 10, 21),
    )


async def hold_only_before_send(world):
    unsent = await approved(world)
    await world.module.hold_draft_invoice.execute(unsent.document_id, "accountant-a", "Wrong")
    assert await status(world, unsent) == "blocked"
    sent = await approved(world)
    world.provider.script_create(sent.reference, TRANSIENT)
    await world.issue()
    world.past_lease()
    with pytest.raises(HoldNotAllowed, match="send_started"):
        await world.module.hold_draft_invoice.execute(sent.document_id, "accountant-a", "Wrong")
    pending = await approved(world)
    await world.issue()
    with pytest.raises(HoldNotAllowed, match="send_started"):
        await world.module.hold_draft_invoice.execute(pending.document_id, "accountant-a", "x")


ROWS = {
    "queued_unapproved_waits_offline": queued_unapproved_waits_offline,
    "awaiting_approve_hold_or_alert": awaiting_approve_hold_or_alert,
    "changed_content_drops_approval": changed_content_drops_approval,
    "approved_looks_up_then_sends_once": approved_looks_up_then_sends_once,
    "approved_lookup_outcomes": approved_lookup_outcomes,
    "approved_create_outcomes": approved_create_outcomes,
    "pending_check_outcomes": pending_check_outcomes,
    "held_draft_resolutions": held_draft_resolutions,
    "failed_retry_outcomes": failed_retry_outcomes,
    "failed_marked_external": failed_marked_external,
    "hold_only_before_send": hold_only_before_send,
}


@pytest.mark.parametrize("row", list(ROWS))
async def test_matrix_manual_rows(world, row):
    await ROWS[row](world)
    assert all(post.payload.get("is_draft", False) is False for post in world.provider.posts())


async def test_stale_hash_rebuilds_request_and_reapproval_sends_new_content(world):
    settled = await approved(world)
    doc = await world.doc(settled.document_id)
    content_b = dict(doc.document)
    content_b["buyer"] = {"name": "Buyer B", "address": "Street B"}
    content_b["lines"] = [dict(doc.document["lines"][0]) | {"name": "Item B", "unit": "box"}]
    await world.set_columns(settled.document_id, document=content_b)
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at) == ("awaiting_approval", None)
    assert doc.request["buyer"] == {"name": "Buyer B", "address": "Street B"}
    await world.approve(settled, actor="accountant-b")
    await world.issue()
    [post] = world.provider.posts()
    assert post.payload["buyer"] == {"name": "Buyer B", "address": "Street B"}
    assert [(item["item_name"], item["unit"]) for item in post.payload["items"]] == [
        ("Item B", "box")
    ]
    assert post.payload == (await world.doc(settled.document_id)).request


async def test_hold_refused_after_lost_create_then_retry(world):
    settled = await approved(world)
    world.provider.script_create(settled.reference, TRANSIENT, TRANSIENT)
    for _ in range(3):
        await world.issue()
        world.past_lease()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.last_error_code) == ("failed", "max_attempts_exceeded")
    # The only way back is a lookup-first retry; there is no attach that skips the lookup.
    with pytest.raises(ResolutionNotAllowed):
        await world.resolve(
            settled.document_id,
            "attach_draft",
            draft=world.draft_command(settled.host_ref_id, settled.intent_id, mode=MANUAL),
        )
    await world.resolve(settled.document_id, "retry")
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.attempts, doc.send_started_at is not None) == ("approved", 0, True)
    for _ in range(2):
        with pytest.raises(HoldNotAllowed, match="send_started"):
            await world.module.hold_draft_invoice.execute(settled.document_id, "accountant-a", "x")
        world.restart()
    assert await status(world, settled) == "approved"


async def test_repair_and_rotation_clear_approval_with_audit(world):
    settled = await approved(world)
    world.provider.script_create(settled.reference, rejected("validation_error"))
    await world.issue()
    await world.resolve(
        settled.document_id,
        "attach_draft",
        draft=world.draft_command(
            settled.host_ref_id, settled.intent_id, mode=MANUAL, name="Fixed"
        ),
    )
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at) == ("queued", None)
    [case] = await world.cases(settled.document_id)
    assert case.details["previous_approvals"][0]["approved_by"] == "accountant-a"
    await world.issue()
    assert await status(world, settled) == "awaiting_approval"
    await world.approve(settled, actor="accountant-b")
    world.provider.script_create(settled.reference, CONFLICT)
    await world.issue()
    await world.resolve(
        settled.document_id, "retry_with_new_reference", note="Reconciled at the portal"
    )
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at, doc.approved_reference_code) == ("queued", None, None)
    [second_case] = [
        case
        for case in await world.cases(settled.document_id)
        if case.details["reasons"][0]["reason"] == "DOCUMENT_EXISTED_UNRESOLVED"
    ]
    assert second_case.details["previous_approvals"][-1]["approved_by"] == "accountant-b"
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.approved_at) == ("awaiting_approval", None)


async def test_approved_across_midnight_sends_send_day_date_and_keeps_approved_at(world):
    world.clock.set(vn(2026, 10, 21, 23, 0))
    settled = await awaiting(world)
    world.clock.set(vn(2026, 10, 21, 23, 59))
    await world.approve(settled)
    world.provider.script_create(settled.reference, QUOTA)
    await world.issue()
    world.advance(timedelta(hours=1, seconds=1))
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    first, second = world.provider.posts()
    doc = await world.doc(settled.document_id)
    assert (issued(first), issued(second)) == (date(2026, 10, 21), date(2026, 10, 22))
    assert (doc.status, doc.issued_date, doc.approved_at) == (
        "issued",
        date(2026, 10, 22),
        vn(2026, 10, 21, 23, 59),
    )


async def test_lost_create_response_after_approval_recovers_by_lookup_one_post(world):
    settled = await approved(world)
    world.provider.script_create(settled.reference, LOST_RESPONSE)
    await world.issue()
    assert await status(world, settled) == "approved"
    world.past_lease()
    await world.issue()
    doc = await world.doc(settled.document_id)
    assert doc.status == "issued" and len(world.provider.posts()) == 1
    assert world.provider.effective_creates[settled.reference] == 1


async def test_no_path_uses_removed_edges(world, transitions):
    for name in ROWS:
        await ROWS[name](world)
    used = set(transitions.edges)
    assert used & REMOVED_EDGES == set()
    # The scenarios really walked the manual graph.
    assert {
        ("queued", "awaiting_approval"),
        ("awaiting_approval", "approved"),
        ("approved", "pending_provider"),
        ("approved", "awaiting_approval"),
        ("blocked", "awaiting_approval"),
        ("failed", "approved"),
    } <= used


async def test_document_row_delivery_mode_is_enum(world):
    auto = await world.settle()
    manual = await world.settle(mode=MANUAL)
    pending = await world.settle(draft=False)
    async with world.crash() as uow:
        rows = {name: await uow.documents.get(s.document_id) for name, s in (
            ("auto", auto), ("manual", manual), ("pending", pending))}  # fmt: skip
        listed = await uow.documents.list_by_status(rows["manual"].status)
    assert isinstance(rows["auto"].delivery_mode, DeliveryMode)
    assert rows["auto"].delivery_mode is DeliveryMode.AUTO
    assert rows["manual"].delivery_mode is DeliveryMode.MANUAL
    assert rows["pending"].delivery_mode is None
    assert all(isinstance(row.delivery_mode, DeliveryMode) for row in listed)


async def test_manual_never_calls_issue_endpoint(world):
    for name in (
        "approved_looks_up_then_sends_once",
        "approved_create_outcomes",
        "pending_check_outcomes",
        "failed_retry_outcomes",
    ):
        await ROWS[name](world)
    await world.artifacts()
    assert set(world.provider.ops()) <= {
        "create",
        "check_create",
        "find_by_reference",
        "fetch_artifacts",
    }
    assert {"create", "check_create", "find_by_reference", "fetch_artifacts"} <= set(
        world.provider.ops()
    )
    assert not hasattr(world.provider, "issue_draft") and not hasattr(world.provider, "check_issue")


async def test_approval_invalidation_logs_full_entry_and_only_writes_open_case(world, caplog):
    settled = await awaiting(world)
    await world.module.hold_draft_invoice.execute(
        settled.document_id, "accountant-a", "Wrong buyer"
    )
    await world.resolve(settled.document_id, "retry")
    [closed] = await world.cases(settled.document_id)
    assert closed.status == "resolved"
    await world.approve(settled)
    await world.set_columns(settled.document_id, approved_snapshot_sha256="0" * 64)
    approved_doc = await world.doc(settled.document_id)
    with caplog.at_level(logging.WARNING, logger=issue_module.__name__):
        await world.issue()
    assert await status(world, settled) == "awaiting_approval"
    # No case is open: the unrelated closed case is left as it was, and the log keeps the
    # whole approval that was dropped.
    assert await world.cases(settled.document_id) == [closed]
    [record] = [r for r in caplog.records if r.message == "invoice_approval_invalidated"]
    assert (
        record.document_id,
        record.approved_by,
        record.approved_at,
        record.approved_snapshot_sha256,
        record.approved_reference_code,
        record.reason,
    ) == (
        str(settled.document_id),
        "accountant-a",
        approved_doc.approved_at.isoformat(),
        "0" * 64,
        settled.reference,
        "snapshot_changed",
    )
    assert not hasattr(record, "buyer")


async def test_move_to_awaiting_approval_requires_matching_generation(world):
    settled = await world.settle(mode=MANUAL)
    now = world.clock.now()
    await world.set_columns(
        settled.document_id,
        lease_generation=5,
        lease_owner="worker-a",
        lease_until=now + timedelta(seconds=60),
        next_attempt_at=now,
    )
    sessions = async_sessionmaker(world.engine, expire_on_commit=False)
    async with sessions() as session:
        documents = DocumentRepository(session, world.tables)
        assert await documents.freeze_request(
            settled.document_id, 5, {"reference_code": settled.reference}, date(2026, 10, 21), now
        )
        assert await documents.move_to_awaiting_approval(settled.document_id, 4, now) is False
        await session.commit()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.lease_owner, doc.next_attempt_at) == ("queued", "worker-a", now)
    async with sessions() as session:
        documents = DocumentRepository(session, world.tables)
        assert await documents.move_to_awaiting_approval(settled.document_id, 5, now) is True
        await session.commit()
    doc = await world.doc(settled.document_id)
    assert (doc.status, doc.next_attempt_at, doc.lease_owner, doc.lease_until) == (
        "awaiting_approval",
        None,
        None,
        None,
    )
