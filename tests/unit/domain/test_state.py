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
