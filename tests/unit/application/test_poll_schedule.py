from datetime import UTC, date, datetime, timedelta

import pytest

from invoice_module.application.config import (
    InvoiceModuleConfig,
    business_days_between,
    next_poll_delay,
    retry_delay,
    today_vn,
)

NOW = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)


def test_schedule_3_5_10_20_30_60_then_hold():
    config = InvoiceModuleConfig()
    delays = [config.next_poll_delay(count, None, NOW).total_seconds() for count in range(9)]
    assert delays == [3, 5, 10, 20, 30, 60, 60, 60, 60]
    assert next_poll_delay(200, None, NOW) == timedelta(seconds=60)


def test_next_retry_at_respected_with_floor():
    assert next_poll_delay(0, NOW + timedelta(seconds=45), NOW) == timedelta(seconds=45)
    assert next_poll_delay(5, NOW + timedelta(seconds=1), NOW) == timedelta(seconds=3)
    assert next_poll_delay(5, NOW - timedelta(minutes=5), NOW) == timedelta(seconds=3)


def test_retry_delay_caps_at_300():
    assert [retry_delay(n).total_seconds() for n in (0, 1, 2, 5, 8, 9, 30)] == [
        1,
        2,
        4,
        32,
        256,
        300,
        300,
    ]


def test_today_vn_crosses_midnight_in_saigon():
    # 16:59:59 UTC is 23:59:59 in Asia/Saigon; one second later the local date changes.
    before = datetime(2026, 10, 21, 16, 59, 59, tzinfo=UTC)
    assert today_vn(before) == date(2026, 10, 21)
    assert today_vn(before + timedelta(seconds=1)) == date(2026, 10, 22)
    assert today_vn(datetime(2026, 10, 21, 0, 0, tzinfo=UTC)) == date(2026, 10, 21)
    with pytest.raises(ValueError):
        today_vn(datetime(2026, 10, 21, 0, 0))


def test_business_days_skip_weekend():
    vn = timedelta(hours=7)
    friday_10 = datetime(2026, 10, 23, 10, 0, tzinfo=UTC) - vn
    assert business_days_between(friday_10, friday_10 + timedelta(hours=23)) == 0
    assert business_days_between(friday_10, friday_10 + timedelta(days=1)) == 0
    assert business_days_between(friday_10, friday_10 + timedelta(days=2)) == 0
    assert business_days_between(friday_10, friday_10 + timedelta(days=3, minutes=-1)) == 0
    assert business_days_between(friday_10, friday_10 + timedelta(days=3)) == 1
    assert business_days_between(friday_10, friday_10 + timedelta(days=4)) == 2
    wednesday_10 = friday_10 - timedelta(days=2)
    assert business_days_between(wednesday_10, wednesday_10 + timedelta(days=1)) == 1
    saturday_10 = friday_10 + timedelta(days=1)
    monday_0 = datetime(2026, 10, 26, 0, 0, tzinfo=UTC) - vn
    assert business_days_between(saturday_10, monday_0 + timedelta(hours=23)) == 0
    assert business_days_between(saturday_10, monday_0 + timedelta(days=1)) == 1


def test_config_rejects_nonpositive_http_timeout():
    for value in (0, 0.0, -1.0):
        with pytest.raises(ValueError, match="http_timeout_s"):
            InvoiceModuleConfig(http_timeout_s=value)
    assert InvoiceModuleConfig(http_timeout_s=0.5).http_timeout_s == 0.5


@pytest.mark.parametrize("value", [0, -1.0])
def test_artifact_timeout_must_be_positive(value):
    assert InvoiceModuleConfig().artifact_timeout_s == 90.0
    with pytest.raises(ValueError, match="artifact_timeout_s"):
        InvoiceModuleConfig(artifact_timeout_s=value)
