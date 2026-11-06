import pytest

from invoice_module.application.config import InvoiceModuleConfig


def test_defaults_keep_the_lease_above_the_http_timeout():
    config = InvoiceModuleConfig()
    assert (config.lease_seconds, config.http_timeout_s) == (60, 20.0)


@pytest.mark.parametrize(
    ("lease_seconds", "http_timeout_s"), [(20, 20.0), (20, 20.5), (10, 20.0), (0, 20.0)]
)
def test_lease_not_above_the_http_timeout_is_refused(lease_seconds, http_timeout_s):
    # No POST could ever be granted: the grant needs the lease to outlast the timeout.
    with pytest.raises(ValueError, match="lease_seconds"):
        InvoiceModuleConfig(lease_seconds=lease_seconds, http_timeout_s=http_timeout_s)


def test_lease_just_above_the_http_timeout_is_accepted():
    config = InvoiceModuleConfig(lease_seconds=21, http_timeout_s=20.0)
    assert config.lease_seconds == 21
