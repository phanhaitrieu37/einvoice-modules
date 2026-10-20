import hashlib
from pathlib import Path
from urllib.parse import urlparse

import pytest

from invoice_module.adapters.storage import ArtifactConflict, FileArtifactStore


def _path(uri: str) -> Path:
    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    return Path(parsed.path)


async def test_put_writes_and_returns_uri(tmp_path):
    store = FileArtifactStore(tmp_path)
    uri = await store.put("tenant/test/account/doc.pdf", b"%PDF-1.4 synthetic", "application/pdf")
    path = _path(uri)
    assert path.is_absolute()
    assert path == (tmp_path / "tenant/test/account/doc.pdf").resolve()
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == hashlib.sha256(b"%PDF-1.4 synthetic").hexdigest()
    )
    # The temporary file was renamed into place; nothing else is left behind.
    assert sorted(item.name for item in path.parent.iterdir()) == ["doc.pdf"]


async def test_put_same_bytes_idempotent(tmp_path):
    store = FileArtifactStore(tmp_path)
    first = await store.put("t/doc.xml", b"<invoice/>", "application/xml")
    mtime = _path(first).stat().st_mtime_ns
    second = await store.put("t/doc.xml", b"<invoice/>", "application/xml")
    assert second == first
    assert _path(second).stat().st_mtime_ns == mtime


async def test_put_different_bytes_conflict(tmp_path):
    store = FileArtifactStore(tmp_path)
    uri = await store.put("t/doc.xml", b"<invoice/>", "application/xml")
    with pytest.raises(ArtifactConflict):
        await store.put("t/doc.xml", b"<other/>", "application/xml")
    assert _path(uri).read_bytes() == b"<invoice/>"


@pytest.mark.parametrize(
    "key", ["../escape.pdf", "t/../../escape.pdf", "/etc/escape.pdf", "t//doc.pdf", ""]
)
async def test_rejects_path_traversal(tmp_path, key):
    store = FileArtifactStore(tmp_path / "root")
    with pytest.raises(ValueError):
        await store.put(key, b"x", "application/pdf")
    assert not (tmp_path / "escape.pdf").exists()
