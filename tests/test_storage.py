from datetime import UTC, datetime, timedelta

from netpulse.models import CheckResult
from netpulse.storage import Storage


def test_latest_returns_most_recent_result_per_target() -> None:
    now = datetime.now(UTC)
    with Storage(":memory:") as storage:
        storage.save(CheckResult("a", ok=False, error="old", checked_at=now - timedelta(minutes=2)))
        storage.save(
            CheckResult(
                "a", ok=True, latency_ms=5.0, status_code=200, ssl_days_left=40, checked_at=now
            )
        )
        storage.save(CheckResult("b", ok=False, error="down", checked_at=now))

        latest = {r.target: r for r in storage.latest()}

    assert latest["a"].ok
    assert latest["a"].latency_ms == 5.0
    assert latest["a"].status_code == 200
    assert latest["a"].ssl_days_left == 40
    assert latest["a"].checked_at == now
    assert not latest["b"].ok
    assert latest["b"].error == "down"


def test_last_states_restores_up_down_flags() -> None:
    now = datetime.now(UTC)
    with Storage(":memory:") as storage:
        storage.save(CheckResult("a", ok=True, checked_at=now))
        storage.save(CheckResult("b", ok=False, checked_at=now))
        assert storage.last_states() == {"a": True, "b": False}


def test_uptime_ratio_only_counts_results_since_cutoff() -> None:
    now = datetime.now(UTC)
    with Storage(":memory:") as storage:
        storage.save(CheckResult("a", ok=False, checked_at=now - timedelta(days=2)))  # outside
        for ok in (True, True, True, False):
            storage.save(CheckResult("a", ok=ok, checked_at=now - timedelta(minutes=1)))
        assert storage.uptime_ratios(now - timedelta(hours=24)) == {"a": 0.75}
