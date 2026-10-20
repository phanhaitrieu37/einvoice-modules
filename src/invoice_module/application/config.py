"""Operational settings of the invoice worker and the date rules it shares with the review queue."""

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Saigon")
_POLL_FLOOR = timedelta(seconds=3)


@dataclass(frozen=True)
class InvoiceModuleConfig:
    lease_seconds: int = 60
    http_timeout_s: float = 20.0
    issue_max_attempts: int = 10
    poll_schedule_s: tuple[int, ...] = (3, 5, 10, 20, 30, 60)
    pending_timeout: timedelta = timedelta(hours=3)
    quota_backoff: timedelta = timedelta(hours=1)
    draft_missing_after: timedelta = timedelta(hours=1)
    stale_alert_after: timedelta = timedelta(minutes=15)
    artifacts_missing_alert_after: timedelta = timedelta(hours=1)
    review_overdue_business_days: int = 1
    previous_reference_recheck_window: timedelta = timedelta(hours=24)
    approval_overdue_business_days: int = 1
    batch_size: int = 20

    def __post_init__(self) -> None:
        # A client timeout never proves the provider stopped working on a request, so there is
        # no timeout/lease inequality to enforce here; the timeout only has to be a real wait.
        if self.http_timeout_s <= 0:
            raise ValueError("http_timeout_s must be positive")
        if not self.poll_schedule_s:
            raise ValueError("poll_schedule_s must not be empty")

    def owner(self) -> str:
        return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"

    def next_poll_delay(
        self, poll_count: int, next_retry_at: datetime | None, now: datetime
    ) -> timedelta:
        return next_poll_delay(poll_count, next_retry_at, now, self.poll_schedule_s)


def next_poll_delay(
    poll_count: int,
    next_retry_at: datetime | None,
    now: datetime,
    schedule: tuple[int, ...] = InvoiceModuleConfig.poll_schedule_s,
) -> timedelta:
    """Delay before the next poll after ``poll_count`` polls; the last step holds.

    A provider hint (``next_retry_at``) wins over the schedule but never comes sooner than
    three seconds.
    """
    if next_retry_at is not None:
        return max(next_retry_at, now + _POLL_FLOOR) - now
    return timedelta(seconds=schedule[min(max(poll_count, 0), len(schedule) - 1)])


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(2 ** max(attempts, 0), 300))


def today_vn(now: datetime) -> date:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(VN_TZ).date()


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole business days (Monday to Friday, Asia/Saigon) from ``start`` to ``end``.

    A business day has elapsed once the same local time is reached on the next weekday; a
    start that falls on a weekend counts from the following Monday at midnight.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("business days need timezone-aware datetimes")
    local_start, local_end = start.astimezone(VN_TZ), end.astimezone(VN_TZ)
    if local_start.weekday() >= 5:
        monday = local_start.date() + timedelta(days=7 - local_start.weekday())
        local_start = datetime.combine(monday, time(0), VN_TZ)
    count = 0
    mark = local_start
    while True:
        mark = _next_business_day(mark)
        if mark > local_end:
            return count
        count += 1


def _next_business_day(moment: datetime) -> datetime:
    day = moment.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, moment.timetz())
