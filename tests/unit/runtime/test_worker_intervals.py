from invoice_module.runtime import WorkerIntervals


def test_default_intervals():
    intervals = WorkerIntervals()
    assert (intervals.issue_s, intervals.poll_s, intervals.artifacts_s) == (1.0, 3.0, 5.0)
