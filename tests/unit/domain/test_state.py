from itertools import pairwise

import pytest

from invoice_module.domain.enums import DocumentStatus
from invoice_module.domain.errors import InvalidTransition
from invoice_module.domain.state import ALLOWED_TRANSITIONS, ensure_transition


def test_every_status_has_transitions_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(DocumentStatus)


def test_terminal_states_have_no_exit() -> None:
    assert not ALLOWED_TRANSITIONS[DocumentStatus.REPLACED]
    assert not ALLOWED_TRANSITIONS[DocumentStatus.CANCELLED_EXTERNALLY]


def test_failed_can_reenter_pending_provider() -> None:
    ensure_transition(DocumentStatus.FAILED, DocumentStatus.PENDING_PROVIDER)


def test_invalid_transition_raises() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(DocumentStatus.ISSUED, DocumentStatus.QUEUED)


MANUAL_RECOVERY_EDGES = [
    (DocumentStatus.QUEUED, DocumentStatus.AWAITING_APPROVAL),
    (DocumentStatus.PENDING_PROVIDER, DocumentStatus.AWAITING_APPROVAL),
    (DocumentStatus.PENDING_PROVIDER, DocumentStatus.APPROVED),
    (DocumentStatus.AWAITING_APPROVAL, DocumentStatus.APPROVED),
    (DocumentStatus.AWAITING_APPROVAL, DocumentStatus.BLOCKED),
    (DocumentStatus.AWAITING_APPROVAL, DocumentStatus.FAILED),
    (DocumentStatus.APPROVED, DocumentStatus.APPROVED),
    (DocumentStatus.APPROVED, DocumentStatus.PENDING_PROVIDER),
    (DocumentStatus.APPROVED, DocumentStatus.ISSUED),
    (DocumentStatus.APPROVED, DocumentStatus.FAILED),
    (DocumentStatus.APPROVED, DocumentStatus.BLOCKED),
    (DocumentStatus.APPROVED, DocumentStatus.AWAITING_APPROVAL),
    (DocumentStatus.BLOCKED, DocumentStatus.AWAITING_APPROVAL),
    (DocumentStatus.BLOCKED, DocumentStatus.FAILED),
    (DocumentStatus.BLOCKED, DocumentStatus.QUEUED),
    (DocumentStatus.BLOCKED, DocumentStatus.NEEDS_DRAFT),
    (DocumentStatus.FAILED, DocumentStatus.AWAITING_APPROVAL),
    (DocumentStatus.FAILED, DocumentStatus.APPROVED),
]


def test_manual_flow_edges() -> None:
    chain = [
        DocumentStatus.QUEUED,
        DocumentStatus.PENDING_PROVIDER,
        DocumentStatus.AWAITING_APPROVAL,
        DocumentStatus.APPROVED,
        DocumentStatus.PENDING_PROVIDER,
        DocumentStatus.ISSUED,
    ]
    for current, target in pairwise(chain):
        ensure_transition(current, target)


@pytest.mark.parametrize(("current", "target"), MANUAL_RECOVERY_EDGES)
def test_manual_recovery_edges_match_matrix(
    current: DocumentStatus, target: DocumentStatus
) -> None:
    ensure_transition(current, target)


def test_awaiting_approval_targets_are_exact() -> None:
    assert ALLOWED_TRANSITIONS[DocumentStatus.AWAITING_APPROVAL] == {
        DocumentStatus.APPROVED,
        DocumentStatus.BLOCKED,
        DocumentStatus.FAILED,
    }


def test_allowed_transitions_are_exact() -> None:
    expected = {
        DocumentStatus.NEEDS_DRAFT: {DocumentStatus.QUEUED, DocumentStatus.BLOCKED},
        DocumentStatus.BLOCKED: {
            DocumentStatus.QUEUED,
            DocumentStatus.NEEDS_DRAFT,
            DocumentStatus.BLOCKED,
            DocumentStatus.AWAITING_APPROVAL,
            DocumentStatus.FAILED,
        },
        DocumentStatus.QUEUED: {
            DocumentStatus.PENDING_PROVIDER,
            DocumentStatus.QUEUED,
            DocumentStatus.FAILED,
            DocumentStatus.ISSUED,
            DocumentStatus.BLOCKED,
            DocumentStatus.AWAITING_APPROVAL,
        },
        DocumentStatus.PENDING_PROVIDER: {
            DocumentStatus.PENDING_PROVIDER,
            DocumentStatus.ISSUED,
            DocumentStatus.FAILED,
            DocumentStatus.AWAITING_APPROVAL,
            DocumentStatus.APPROVED,
        },
        DocumentStatus.AWAITING_APPROVAL: {
            DocumentStatus.APPROVED,
            DocumentStatus.BLOCKED,
            DocumentStatus.FAILED,
        },
        DocumentStatus.APPROVED: {
            DocumentStatus.APPROVED,
            DocumentStatus.PENDING_PROVIDER,
            DocumentStatus.ISSUED,
            DocumentStatus.FAILED,
            DocumentStatus.BLOCKED,
            DocumentStatus.AWAITING_APPROVAL,
        },
        DocumentStatus.FAILED: {
            DocumentStatus.QUEUED,
            DocumentStatus.PENDING_PROVIDER,
            DocumentStatus.ISSUED,
            DocumentStatus.AWAITING_APPROVAL,
            DocumentStatus.APPROVED,
        },
        DocumentStatus.ISSUED: {DocumentStatus.REPLACED, DocumentStatus.CANCELLED_EXTERNALLY},
        DocumentStatus.REPLACED: set(),
        DocumentStatus.CANCELLED_EXTERNALLY: set(),
    }
    assert expected == ALLOWED_TRANSITIONS


def test_queued_cannot_skip_approval() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(DocumentStatus.QUEUED, DocumentStatus.APPROVED)


def test_approved_cannot_return_to_queued() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(DocumentStatus.APPROVED, DocumentStatus.QUEUED)
    with pytest.raises(InvalidTransition):
        ensure_transition(DocumentStatus.APPROVED, DocumentStatus.NEEDS_DRAFT)
