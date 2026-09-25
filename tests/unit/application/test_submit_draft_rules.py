from decimal import Decimal
from uuid import uuid4

import pytest

from invoice_module.application.submit_draft import SubmitDraft, SubmitDraftCommand
from invoice_module.domain.document import Buyer
from invoice_module.domain.errors import InvalidLine, InvalidTaxCode, TotalMismatch


class NeverOpened:
    def __call__(self):
        raise AssertionError("validation must finish before opening a unit of work")


def command(**over):
    values = {
        "tenant_id": "tenant-test",
        "merchant_id": uuid4(),
        "host_ref_type": "order",
        "host_ref_id": "order-1",
        "intent_id": uuid4(),
        "lines": (
            {
                "name": "Service",
                "unit": "each",
                "quantity": Decimal("1"),
                "unit_price_vnd": 100,
                "tax_rate": 0,
            },
        ),
        "declared_total_vnd": 100,
    }
    values.update(over)
    return SubmitDraftCommand(**values)


@pytest.mark.asyncio
async def test_total_mismatch_rejected_before_db():
    with pytest.raises(TotalMismatch):
        await SubmitDraft(NeverOpened(), None).execute(command(declared_total_vnd=101))


@pytest.mark.asyncio
async def test_invalid_tax_code_rejected_before_db():
    with pytest.raises(InvalidTaxCode):
        await SubmitDraft(NeverOpened(), None).execute(
            command(buyer=Buyer(name="Company", tax_code="invalid", address="Test"))
        )


@pytest.mark.asyncio
async def test_line_without_name_rejected_before_db():
    cmd = command(
        lines=(
            {
                "name": "",
                "unit": "each",
                "quantity": Decimal("1"),
                "unit_price_vnd": 100,
                "tax_rate": 0,
            },
        )
    )
    with pytest.raises(InvalidLine) as caught:
        await SubmitDraft(NeverOpened(), None).execute(cmd)
    assert caught.value.line_index == 0


@pytest.mark.asyncio
async def test_invalid_line_is_reindexed_from_source_position():
    valid = command().lines[0]
    broken = dict(valid) | {"name": ""}
    cmd = command(lines=(valid, valid, broken))
    with pytest.raises(InvalidLine) as caught:
        await SubmitDraft(NeverOpened(), None).execute(cmd)
    assert caught.value.line_index == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [100.0, True, 0, -1])
async def test_declared_total_requires_positive_integer_vnd_before_db(value):
    error = TypeError if isinstance(value, (float, bool)) else ValueError
    with pytest.raises(error):
        await SubmitDraft(NeverOpened(), None).execute(command(declared_total_vnd=value))
