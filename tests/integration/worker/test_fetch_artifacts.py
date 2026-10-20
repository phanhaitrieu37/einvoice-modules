import hashlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.integration


async def issued(world):
    settled = await world.settle()
    await world.issue()
    world.advance(timedelta(seconds=3))
    await world.poll()
    assert (await world.doc(settled.document_id)).status == "issued"
    return settled


async def test_stores_pdf_and_xml_with_sha256(world):
    settled = await issued(world)
    [result] = await world.artifacts()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "stored"
    for uri, digest, extension in (
        (doc.pdf_uri, doc.pdf_sha256, "pdf"),
        (doc.xml_uri, doc.xml_sha256, "xml"),
    ):
        path = Path(urlparse(uri).path)
        assert path.name == f"{doc.id}.{extension}"
        assert path.parent.parts[-3:] == (
            doc.tenant_id,
            doc.environment,
            str(doc.issuer_account_id),
        )
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert (doc.status, doc.next_attempt_at, doc.lease_owner) == ("issued", None, None)
    assert await world.artifacts() == []


async def test_fetch_failure_keeps_issued_and_backs_off(world):
    settled = await issued(world)
    world.provider.fail_artifacts(settled.reference)
    [result] = await world.artifacts()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "backoff"
    assert (doc.status, doc.last_error_code, doc.pdf_uri) == (
        "issued",
        "artifact_fetch_failed",
        None,
    )
    assert doc.next_attempt_at == world.clock.now() + timedelta(seconds=2)
    assert await world.artifacts() == []
    world.advance(timedelta(seconds=2))
    [result] = await world.artifacts()
    doc = await world.doc(settled.document_id)
    assert result.outcome == "stored" and doc.status == "issued" and doc.last_error_code is None


async def test_documents_without_issue_are_not_claimed(world):
    done = await issued(world)
    await world.artifacts()
    pending = await world.settle()
    failed = await world.settle()
    await world.issue()
    queued = await world.settle()
    await world.set_columns(failed.document_id, status="failed")
    statuses = {}
    for settled in (queued, pending, failed):
        await world.set_columns(
            settled.document_id, next_attempt_at=world.clock.now(), lease_until=None
        )
        statuses[settled.document_id] = (await world.doc(settled.document_id)).status
    assert sorted(statuses.values()) == ["failed", "pending_provider", "queued"]
    assert await world.artifacts() == []
    assert [c.reference_code for c in world.provider.calls if c.op == "fetch_artifacts"] == [
        done.reference
    ]
