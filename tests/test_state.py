import pytest

from netpulse.models import CheckResult
from netpulse.state import Event, StateTracker, classify


def test_first_failure_alerts(down: CheckResult) -> None:
    assert StateTracker().update(down) is Event.WENT_DOWN


def test_first_success_is_silent(up: CheckResult) -> None:
    assert StateTracker().update(up) is None


def test_no_repeat_alerts_while_down(down: CheckResult) -> None:
    tracker = StateTracker()
    assert tracker.update(down) is Event.WENT_DOWN
    assert tracker.update(down) is None
    assert tracker.update(down) is None


def test_recovery_alerts_once(up: CheckResult, down: CheckResult) -> None:
    tracker = StateTracker()
    tracker.update(down)
    assert tracker.update(up) is Event.RECOVERED
    assert tracker.update(up) is None


def test_flapping_alerts_on_every_change(up: CheckResult, down: CheckResult) -> None:
    tracker = StateTracker({"svc": True})
    events = [tracker.update(r) for r in (down, up, down, up)]
    assert events == [Event.WENT_DOWN, Event.RECOVERED, Event.WENT_DOWN, Event.RECOVERED]


def test_restored_down_state_does_not_realert(up: CheckResult, down: CheckResult) -> None:
    tracker = StateTracker({"svc": False})
    assert tracker.update(down) is None
    assert tracker.update(up) is Event.RECOVERED


def test_targets_are_tracked_independently(down: CheckResult) -> None:
    tracker = StateTracker()
    other = CheckResult("other", ok=False, error="x")
    assert tracker.update(down) is Event.WENT_DOWN
    assert tracker.update(other) is Event.WENT_DOWN


def slow(ok: bool = True) -> CheckResult:
    return CheckResult("svc", ok=ok, latency_ms=900.0, degraded=ok, error=None if ok else "boom")


@pytest.mark.parametrize(
    ("latency", "threshold", "degraded"),
    [
        (100.0, 500.0, False),
        (500.0, 500.0, False),  # exactly at the threshold is still fine
        (500.1, 500.0, True),
        (9000.0, None, False),  # no threshold configured
        (None, 500.0, False),  # no latency measured
    ],
)
def test_classify_marks_only_results_slower_than_the_threshold(
    latency: float | None, threshold: float | None, degraded: bool
) -> None:
    result = CheckResult("svc", ok=True, latency_ms=latency)

    assert classify(result, threshold).degraded is degraded


def test_classify_never_touches_a_failed_result() -> None:
    result = CheckResult("svc", ok=False, latency_ms=9000.0, error="boom")

    assert classify(result, 100.0) == result


def test_slowdown_is_reported_once_and_so_is_the_return_to_normal(up: CheckResult) -> None:
    tracker = StateTracker({"svc": True})

    events = [tracker.update(r) for r in (up, slow(), slow(), up, up)]

    assert events == [None, Event.DEGRADED, None, Event.NORMALIZED, None]


def test_first_ever_check_being_slow_is_reported(up: CheckResult) -> None:
    assert StateTracker().update(slow()) is Event.DEGRADED
    assert StateTracker().update(up) is None


def test_degraded_target_that_goes_down_is_down_not_degraded(down: CheckResult) -> None:
    tracker = StateTracker({"svc": True}, degraded={"svc"})

    assert tracker.update(down) is Event.WENT_DOWN


def test_recovering_into_a_slow_state_reports_recovery_then_normalisation(
    up: CheckResult,
) -> None:
    tracker = StateTracker({"svc": False})

    events = [tracker.update(r) for r in (slow(), slow(), up)]

    assert events == [Event.RECOVERED, None, Event.NORMALIZED]


def test_restored_degraded_state_does_not_realert(up: CheckResult) -> None:
    tracker = StateTracker({"svc": True}, degraded={"svc"})

    assert tracker.update(slow()) is None
    assert tracker.update(up) is Event.NORMALIZED


def test_forgotten_target_is_judged_afresh(up: CheckResult, down: CheckResult) -> None:
    tracker = StateTracker({"svc": False}, degraded={"svc"})

    tracker.forget("svc")

    assert tracker.update(down) is Event.WENT_DOWN  # not "already down"
    tracker.forget("svc")
    assert tracker.update(up) is None  # not a recovery
    tracker.forget("never-seen")  # harmless
