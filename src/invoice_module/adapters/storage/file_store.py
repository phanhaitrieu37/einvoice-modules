"""Local artifact store: atomic writes under one root, idempotent for identical bytes."""

import asyncio
import os
import tempfile
from pathlib import Path


class ArtifactConflict(Exception):
    """A different file already exists under the key; artifacts are never overwritten."""


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        return await asyncio.to_thread(self._put, key, data)

    def _path(self, key: str) -> Path:
        parts = key.split("/")
        if not key or key.startswith("/") or "\\" in key or ".." in parts or "" in parts:
            raise ValueError("artifact key must be a relative path without '..'")
        path = (self._root / Path(*parts)).resolve()
        if self._root not in path.parents:
            raise ValueError("artifact key escapes the store root")
        return path

    def _put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # A hard link publishes the complete file and never replaces one that exists, so two
            # writers cannot overwrite each other.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise ArtifactConflict(key) from None
                return path.as_uri()
            _fsync_directory(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path.as_uri()


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
