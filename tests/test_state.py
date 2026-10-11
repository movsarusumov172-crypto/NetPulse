from netpulse.models import CheckResult
from netpulse.state import Event, StateTracker


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
