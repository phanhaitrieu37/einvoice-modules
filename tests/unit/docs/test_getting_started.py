"""The bilingual getting-started guide: structure, runnable code, the runbook tables."""

import re
from pathlib import Path

import pytest

from invoice_module.application.resolve_review import ALLOWED_RESOLUTIONS
from invoice_module.domain.enums import DocumentStatus, ReviewReason

ROOT = Path(__file__).parents[3]
EN, VI = "docs/getting-started.md", "docs/getting-started.vi.md"
EXPECTED_SECTIONS_EN = [
    "1. Install",
    "2. Create the schema",
    "3. Credentials",
    "4. Build the module",
    "5. Onboard an issuer account",
    "6. Submit the draft with CreateIntent",
    "7. Wire the outbox",
    "8. Run the worker",
    "9. Operate the review queue",
    "10. Cancellation",
    "11. Artifacts and backup",
    "12. Alerts and coverage queries",
    "13. Known limitations",
    "14. Approve manual-delivery invoices",
]
EXPECTED_SECTIONS_VI = [
    "1. Cài đặt",
    "2. Tạo schema",
    "3. Thông tin xác thực",
    "4. Dựng module",
    "5. Đăng ký tài khoản xuất hoá đơn",
    "6. Nộp bản nháp cùng CreateIntent",
    "7. Nối outbox",
    "8. Chạy worker",
    "9. Vận hành hàng đợi review",
    "10. Huỷ hoá đơn",
    "11. Tệp hoá đơn và sao lưu",
    "12. Cảnh báo và truy vấn độ phủ",
    "13. Giới hạn đã biết",
    "14. Duyệt hoá đơn xuất thủ công",
]
BOTH = [(EN, EXPECTED_SECTIONS_EN), (VI, EXPECTED_SECTIONS_VI)]
# HTTP status and code of every SePay error the runbook must name (docs ``xu-ly-loi`` and the
# create page), plus the plain 401 and code-less 5xx rows.
DOCUMENTED_ERROR_CODES = [
    "400 ISSUED_DATE_INVALID",
    "400 VALIDATION_ERROR",
    "400 SELLER_STORE_NOT_FOUND",
    "400 SELLER_STORE_INACTIVE",
    "422 INVOICE_DATE_BEFORE_TAX_APPROVAL",
    "422 REGISTRATION_NOT_TAX_APPROVED",
    "409 EINVOICE_DOCUMENT_EXISTED",
    "500 QUOTA_HAS_BEEN_USERD_UP",
    "500 REGISTRATION_NOT_COMPLETE",
    "500 BILLING_IS_UNPAID",
    "500 EINVOICE_ACCOUNT_NOT_FOUND",
    "`401`",
    "`5xx` ",
]
CODE_BLOCK = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
TICKED = re.compile(r"`([^`]+)`")
ANY_STATUS = "any"


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def sections(path: str) -> dict[str, str]:
    """``## `` heading -> body, in document order, ignoring headings inside code blocks."""
    body = CODE_BLOCK.sub(lambda match: "\n" * match.group(0).count("\n"), text(path))
    parts = re.split(r"^## (.+)$", body, flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def section(path: str, number: int) -> str:
    [body] = [body for title, body in sections(path).items() if title.startswith(f"{number}. ")]
    return body


def code_blocks(path: str) -> list[tuple[str, str]]:
    return [(match.group(1), match.group(2)) for match in CODE_BLOCK.finditer(text(path))]


def table_rows(body: str) -> list[list[str]]:
    rows = []
    for line in body.splitlines():
        if line.startswith("|") and not re.fullmatch(r"\|[\s|:-]+\|", line.strip()):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def table_count(path: str) -> int:
    body = CODE_BLOCK.sub("", text(path))
    return len(re.findall(r"^\|[ :-]*-{3}", body, flags=re.MULTILINE))


def matrix_triples(path: str) -> set[tuple[str, str, str]]:
    """(reason, status, resolution) from the rows of section 9 whose first cell is a reason."""
    reasons = {reason.value for reason in ReviewReason}
    triples = set()
    for row in table_rows(section(path, 9)):
        first = TICKED.findall(row[0])
        if len(row) < 3 or len(first) != 1 or first[0] not in reasons:
            continue
        statuses = TICKED.findall(row[1])
        if statuses == [ANY_STATUS]:
            statuses = [status.value for status in DocumentStatus]
        triples |= {
            (first[0], status, resolution)
            for status in statuses
            for resolution in TICKED.findall(row[2])
        }
    return triples


def without_comments(language: str, code: str) -> str:
    marker = "--" if language == "sql" else "#"
    lines = (re.sub(rf"(^|\s+){re.escape(marker)}.*$", "", line) for line in code.splitlines())
    return "\n".join(line for line in lines if line.strip())


@pytest.mark.parametrize(("path", "expected"), BOTH)
def test_sections_present(path, expected):
    assert list(sections(path)) == expected


@pytest.mark.parametrize(("path", "expected"), BOTH)
def test_python_blocks_compile(path, expected):
    blocks = [code for language, code in code_blocks(path) if language == "python"]
    assert len(blocks) >= 7
    for index, code in enumerate(blocks):
        compile(code, f"{path}:python-block-{index}", "exec")


@pytest.mark.parametrize(("path", "expected"), BOTH)
def test_no_secret_values(path, expected):
    content = text(path)
    assert "eyJ" not in content
    assert re.search(r"(?i)client_secret\s*[=:]\s*['\"]?[A-Za-z0-9]", content) is None
    assert re.search(r"(?i)_CLIENT_(ID|SECRET)=\S", content) is None
    assert re.search(r"einvoice-api[.]sepay[.]vn", content) is None


@pytest.mark.parametrize(("path", "expected"), BOTH)
def test_review_table_lists_every_reason(path, expected):
    runbook = section(path, 9)
    assert all(f"`{reason.value}`" in runbook for reason in ReviewReason)
    documented = matrix_triples(path)
    matrix = {
        (reason.value, status.value, resolution.value)
        for (reason, status), resolutions in ALLOWED_RESOLUTIONS.items()
        for resolution in resolutions
    }
    assert documented - matrix == set(), "pairs the module does not allow"
    assert matrix - documented == set(), "allowed pairs the runbook leaves out"


def test_both_languages_have_same_structure():
    assert len(sections(EN)) == len(sections(VI)) == 14
    assert table_count(EN) == table_count(VI)
    assert len(table_rows(text(EN))) == len(table_rows(text(VI)))
    en, vi = code_blocks(EN), code_blocks(VI)
    assert [language for language, _ in en] == [language for language, _ in vi]
    assert [without_comments(*block) for block in en] == [without_comments(*block) for block in vi]
    commands = re.compile(r"python -m invoice_module\.cli \w+ \w+|uv pip install \S+|^def \w+|"
                          r"^async def \w+", re.MULTILINE)  # fmt: skip
    assert set(commands.findall(text(EN))) == set(commands.findall(text(VI)))
    assert matrix_triples(EN) == matrix_triples(VI)


@pytest.mark.parametrize(("path", "expected"), BOTH)
def test_runbook_lists_documented_error_codes(path, expected):
    runbook = section(path, 9)
    missing = [code for code in DOCUMENTED_ERROR_CODES if code not in runbook]
    assert missing == []
    assert "`item_code`" in section(path, 6)
    assert "254/2026" in text(path)
