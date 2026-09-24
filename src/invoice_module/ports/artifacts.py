from typing import Protocol


class ArtifactStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str: ...
