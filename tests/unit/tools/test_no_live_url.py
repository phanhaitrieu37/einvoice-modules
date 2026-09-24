import re
from pathlib import Path

from tools.einvoice_probe.client import SANDBOX_BASE_URL

URL = re.compile(r"https://[a-z0-9.-]*sepay\.vn")
ROOT = Path(__file__).parents[3]


def test_only_sandbox_einvoice_host_in_repo() -> None:
    found: set[str] = set()
    for folder in ("src", "tools", "examples", "docs"):
        path = ROOT / folder
        if not path.exists():
            continue
        for file in path.rglob("*"):
            if file.is_file():
                found.update(URL.findall(file.read_text(encoding="utf-8", errors="ignore")))
    einvoice_urls = {url for url in found if "einvoice-api" in url}
    assert einvoice_urls <= {SANDBOX_BASE_URL}
