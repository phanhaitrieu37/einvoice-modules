"""Safe helpers for reading probe environment files without exposing their values."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path

REQUIRED_KEYS = (
    "SEPAY_EINVOICE_ENVIRONMENT",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_ID",
    "SEPAY_EINVOICE_SANDBOX_CLIENT_SECRET",
)


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines, supporting comments and shell-style quotes."""
    result: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                parsed = shlex.split(value, comments=True, posix=True)
            except ValueError:
                parsed = []
            value = parsed[0] if parsed else ""
        else:
            value = value.split("#", 1)[0].strip()
        result[key] = value
    return result


def preflight(env: Mapping[str, str]) -> list[str]:
    """Return presence only; never include a configured value."""
    lines = [f"{key}: {'present' if env.get(key) else 'missing'}" for key in REQUIRED_KEYS]
    environment = env.get("SEPAY_EINVOICE_ENVIRONMENT", "")
    if environment:
        lines.append(f"environment: {environment}")
    return lines
