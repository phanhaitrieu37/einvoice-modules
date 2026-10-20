"""Adapter behaviour proven on the SePay eInvoice sandbox (never run in CI).

Create budget per full run: s1 one create, s2 two (one accepted, one duplicate), s4 and s6
none, the issue-date rule at most two.
"""

import hashlib
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from invoice_module.adapters.sepay.endpoints import ENDPOINTS
from invoice_module.domain.document import Buyer
from invoice_module.ports.provider import (
    DocumentExists,
    ProviderError,
    ProviderRejected,
)

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.timeout(900),
    pytest.mark.asyncio(loop_scope="module"),
]

BLANK_ADDRESS_STATES = {"absent", "null", "empty"}
SYNTHETIC_BUYER = Buyer(name="Sandbox Buyer", email="sandbox-buyer@example.com")


def _address_state(buyer: Any) -> str:
    if not isinstance(buyer, dict) or "address" not in buyer:
        return "absent"
    value = buyer["address"]
    if value is None:
        return "null"
    return "empty" if value == "" else "present"


def _lookup_path(reference: str) -> str:
    return ENDPOINTS["get_by_reference"][1].replace("{reference_code}", reference)


def _check_path(tracking_code: str) -> str:
    return ENDPOINTS["check_create"][1].replace("{tracking_code}", tracking_code)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def example_invoice(sandbox: Any) -> dict[str, Any]:
    """One issued invoice with a synthetic buyer, shared by the duplicate and download tests."""
    reference = str(uuid4())
    document = sandbox.document(SYNTHETIC_BUYER)
    request = sandbox.provider.build_create_payload(
        sandbox.account, document, reference, sandbox.today()
    )
    accepted = await sandbox.provider.create(sandbox.account, sandbox.creds, request)
    assert accepted.tracking_code
    progress, polls = await sandbox.poll(accepted.tracking_code)
    assert progress.state == "success", progress
    assert progress.invoice_number
    return {
        "reference": reference,
        "request": request,
        "document": document,
        "progress": progress,
        "polls": polls,
    }


async def test_s1_create_and_poll_until_issued(sandbox: Any) -> None:
    reference = str(uuid4())
    document = sandbox.document(None)
    assert document.buyer == {
        "name": sandbox.config["buyer_fallback_name"],
        "address": sandbox.config["buyer_fallback_address"],
    }
    issued_date = sandbox.today()
    request = sandbox.provider.build_create_payload(
        sandbox.account, document, reference, issued_date
    )
    assert "address" not in request["buyer"]
    assert "type" not in request["buyer"]
    assert request["is_draft"] is False
    creates_before = len(sandbox.creates())
    accepted = await sandbox.provider.create(sandbox.account, sandbox.creds, request)
    assert len(sandbox.creates()) == creates_before + 1
    assert accepted.tracking_code
    progress, polls = await sandbox.poll(accepted.tracking_code)
    assert progress.state == "success", progress
    assert progress.invoice_number
    assert progress.issued_date == issued_date

    check_body = await sandbox.raw_get(_check_path(accepted.tracking_code))
    lookup_body = await sandbox.raw_get(_lookup_path(reference))
    check_address = _address_state(check_body["data"]["invoice"].get("buyer"))
    lookup_address = _address_state(lookup_body["data"].get("buyer"))
    invoice = await sandbox.provider.find_by_reference(sandbox.account, sandbox.creds, reference)
    sandbox.evidence(
        "s1",
        {
            "create_count": len(sandbox.creates()) - creates_before,
            "create_status": sandbox.creates()[-1].status,
            "polls": polls,
            "final_state": progress.state,
            "invoice_number_present": progress.invoice_number is not None,
            "issued_date_sent": issued_date.isoformat(),
            "issued_date_check": str(progress.issued_date),
            "issued_date_lookup": str(invoice.issued_date if invoice else None),
            "check_buyer_address": check_address,
            "lookup_buyer_address": lookup_address,
            "lookup_total_matches": invoice is not None
            and invoice.total_amount_vnd == document.total_vnd,
        },
    )
    assert check_address in BLANK_ADDRESS_STATES
    assert lookup_address in BLANK_ADDRESS_STATES
    assert invoice is not None
    assert invoice.state == "success"
    assert invoice.issued_date == issued_date
    assert invoice.invoice_number == progress.invoice_number
    assert invoice.total_amount_vnd == document.total_vnd


async def test_s2_duplicate_reference_409_then_find_by_reference(
    sandbox: Any, example_invoice: dict[str, Any]
) -> None:
    creates_before = len(sandbox.creates())
    with pytest.raises(DocumentExists):
        await sandbox.provider.create(sandbox.account, sandbox.creds, example_invoice["request"])
    duplicate = sandbox.creates()[-1]
    assert len(sandbox.creates()) == creates_before + 1
    invoice = await sandbox.provider.find_by_reference(
        sandbox.account, sandbox.creds, example_invoice["reference"]
    )
    sandbox.evidence(
        "s2",
        {
            "first_create_polls": example_invoice["polls"],
            "duplicate_status": duplicate.status,
            "duplicate_error_code": duplicate.error_code,
            "lookup_state": invoice.state if invoice else None,
            "lookup_number_matches": invoice is not None
            and invoice.invoice_number == example_invoice["progress"].invoice_number,
            "lookup_total_matches": invoice is not None
            and invoice.total_amount_vnd == example_invoice["document"].total_vnd,
        },
    )
    assert duplicate.status == 409
    assert duplicate.error_code == "EINVOICE_DOCUMENT_EXISTED"
    assert invoice is not None
    assert invoice.state == "success"
    assert invoice.invoice_number == example_invoice["progress"].invoice_number
    assert invoice.total_amount_vnd == example_invoice["document"].total_vnd


async def test_s4_fetch_pdf_and_xml(sandbox: Any, example_invoice: dict[str, Any]) -> None:
    artifacts = await sandbox.provider.fetch_artifacts(
        sandbox.account, sandbox.creds, example_invoice["reference"], ""
    )
    xml_head = artifacts.xml.lstrip(b"\xef\xbb\xbf").lstrip()
    sandbox.evidence(
        "s4",
        {
            "pdf_size": len(artifacts.pdf),
            "pdf_sha256": hashlib.sha256(artifacts.pdf).hexdigest(),
            "xml_size": len(artifacts.xml),
            "xml_sha256": hashlib.sha256(artifacts.xml).hexdigest(),
            "pdf_magic": artifacts.pdf[:5].decode("latin-1"),
            "xml_starts_with_tag": xml_head.startswith(b"<"),
        },
    )
    assert artifacts.pdf.startswith(b"%PDF")
    assert xml_head.startswith(b"<")


async def test_s6_describe_account_reports_quota(sandbox: Any, findings: dict[str, Any]) -> None:
    facts = await sandbox.provider.describe_account(sandbox.account, sandbox.creds)
    sandbox.evidence(
        "s6",
        {
            "templates": list(facts.templates),
            "series": list(facts.series),
            "quota_type": type(facts.quota_remaining).__name__,
        },
    )
    assert sandbox.account.template_code in facts.templates
    if findings["quota_endpoint"]:
        assert isinstance(facts.quota_remaining, int)
        assert not isinstance(facts.quota_remaining, bool)
    else:
        assert facts.quota_remaining is None


async def test_issued_date_rule_backdated_create_then_same_reference_today(sandbox: Any) -> None:
    """A backdated one-step create, then the same reference dated today (two creates)."""
    reference = str(uuid4())
    document = sandbox.document(SYNTHETIC_BUYER)
    today = sandbox.today()
    yesterday = today - timedelta(days=1)
    record: dict[str, Any] = {"issued_date_first": yesterday.isoformat()}
    creates_before = len(sandbox.creates())

    first = sandbox.provider.build_create_payload(sandbox.account, document, reference, yesterday)
    first_form = None
    try:
        accepted = await sandbox.provider.create(sandbox.account, sandbox.creds, first)
    except ProviderRejected as rejected:
        call = sandbox.creates()[-1]
        record["first"] = {
            "form": "sync_rejected",
            "status": call.status,
            "error_code": call.error_code,
            "error_message": call.error_message,
            "mapped_code": rejected.code,
            "tracking_code_present": False,
        }
        if call.status == 400 and rejected.code == "issued_date_invalid":
            first_form = "sync_400_issued_date_invalid"
    else:
        progress, polls = (
            await sandbox.poll(accepted.tracking_code) if accepted.tracking_code else (None, 0)
        )
        record["first"] = {
            "form": "accepted",
            "status": sandbox.creates()[-1].status,
            "tracking_code_present": accepted.tracking_code is not None,
            "polls": polls,
            "final_state": progress.state if progress else None,
            "provider_message": progress.provider_message if progress else None,
            "issued_date_provider": str(progress.issued_date) if progress else None,
        }
        if progress is not None and progress.state == "failed":
            first_form = "accepted_then_async_failed"
    record["first_form"] = first_form
    if first_form is None:
        record["creates"] = len(sandbox.creates()) - creates_before
        sandbox.evidence("issued_date_rule", record)
        pytest.fail(f"backdated create outcome is not a listed rejection form: {record['first']}")

    second = sandbox.provider.build_create_payload(sandbox.account, document, reference, today)
    record["issued_date_second"] = today.isoformat()
    try:
        accepted = await sandbox.provider.create(sandbox.account, sandbox.creds, second)
    except DocumentExists:
        lookup = await sandbox.provider.find_by_reference(sandbox.account, sandbox.creds, reference)
        record["second"] = {
            "form": "409",
            "status": sandbox.creates()[-1].status,
            "error_code": sandbox.creates()[-1].error_code,
            "lookup_state": lookup.state if lookup else None,
            "lookup_provider_message": lookup.provider_message if lookup else None,
        }
        record["conclusion"] = "rejected create consumed the reference"
    except ProviderError as error:
        call = sandbox.creates()[-1]
        record["second"] = {
            "form": "other",
            "status": call.status,
            "error_code": call.error_code,
            "error_message": call.error_message,
            "error": type(error).__name__,
        }
        record["conclusion"] = "unexpected"
    else:
        progress, polls = await sandbox.poll(accepted.tracking_code)
        record["second"] = {
            "form": "accepted",
            "status": sandbox.creates()[-1].status,
            "polls": polls,
            "final_state": progress.state,
            "invoice_number_present": progress.invoice_number is not None,
            "issued_date_provider": str(progress.issued_date),
        }
        record["conclusion"] = "rejected create did not consume the reference"
    record["creates"] = len(sandbox.creates()) - creates_before
    sandbox.evidence("issued_date_rule", record)
    assert record["creates"] == 2
    assert record["second"]["form"] in {"accepted", "409"}, record["second"]
    if record["second"]["form"] == "accepted":
        assert record["second"]["final_state"] == "success"
        assert record["second"]["issued_date_provider"] == today.isoformat()
