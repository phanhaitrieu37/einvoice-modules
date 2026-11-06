from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from invoice_module.adapters.sqlalchemy.repositories.documents import DocumentRow
from invoice_module.application.queries import GetDocumentStatus
from invoice_module.domain.enums import DocumentStatus

ISSUE_DATE = date(2026, 11, 3)
# The provider's issue date exists only once the provider has issued the invoice; before that
# the column holds the date the module froze into the unsent request.
ISSUED_BY_PROVIDER = {
    DocumentStatus.ISSUED,
    DocumentStatus.REPLACED,
    DocumentStatus.CANCELLED_EXTERNALLY,
}


def document(status: DocumentStatus) -> DocumentRow:
    return DocumentRow(
        id=uuid4(),
        tenant_id="tenant-test",
        merchant_id=uuid4(),
        environment="test",
        settlement_id=uuid4(),
        intent_id=uuid4(),
        host_ref_type="order",
        host_ref_id="order-1",
        issuer_account_id=uuid4(),
        draft_id=uuid4(),
        settled_amount_vnd=100,
        settled_at=datetime(2026, 11, 3, 3, tzinfo=UTC),
        status=status,
        document=None,
        delivery_mode=None,
        approved_at=None,
        approved_by=None,
        approved_snapshot_sha256=None,
        approved_reference_code=None,
        issued_date=ISSUE_DATE,
    )


class _Documents:
    def __init__(self, row: DocumentRow) -> None:
        self.row = row

    async def get_original(self, tenant_id, settlement_id):
        return self.row

    async def get_by_host_ref(self, tenant_id, host_ref_type, host_ref_id):
        return [self.row]


class _ReviewCases:
    async def open_reasons(self, document_id):
        return []


class _UnitOfWork:
    def __init__(self, row: DocumentRow) -> None:
        self.documents, self.review_cases = _Documents(row), _ReviewCases()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.mark.parametrize("status", list(DocumentStatus))
async def test_issued_date_is_shown_only_once_the_provider_issued(status):
    row = document(status)
    query = GetDocumentStatus(lambda: _UnitOfWork(row))
    expected = ISSUE_DATE if status in ISSUED_BY_PROVIDER else None
    by_settlement = await query.by_settlement(row.tenant_id, row.settlement_id)
    [by_host_ref] = await query.by_host_ref(row.tenant_id, row.host_ref_type, row.host_ref_id)
    assert by_settlement is not None
    assert by_settlement.issued_date == by_host_ref.issued_date == expected
    assert by_settlement.status == status
