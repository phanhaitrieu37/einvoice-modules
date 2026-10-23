from invoice_module.runtime import WorkerIntervals


def test_default_intervals():
    intervals = WorkerIntervals()
    assert (intervals.issue_s, intervals.poll_s, intervals.sweep_s, intervals.artifacts_s) == (
        1.0,
        3.0,
        30.0,
        5.0,
    )


def test_intervals_keep_positional_order():
    assert WorkerIntervals(1.5, 2.5, 31.5, 4.5) == WorkerIntervals(
        issue_s=1.5, poll_s=2.5, sweep_s=31.5, artifacts_s=4.5
    )
