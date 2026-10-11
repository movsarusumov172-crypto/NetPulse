import asyncio
import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from netpulse.config import Settings
from netpulse.dashboard import StatusBoard
from netpulse.metrics import Metrics
from netpulse.models import CheckResult, DnsTarget, Target
from netpulse.monitor import CheckFn, Monitor
from netpulse.storage import Storage

TARGET = DnsTarget(name="svc", host="svc.test")


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class ScriptedCheck:
    """Returns one scripted outcome per attempt; the last outcome repeats."""

    def __init__(self, *outcomes: bool) -> None:
        self.outcomes = outcomes
        self.attempts = 0

    async def __call__(self, target: Target) -> CheckResult:
        ok = self.outcomes[min(self.attempts, len(self.outcomes) - 1)]
        self.attempts += 1
        return CheckResult(target.name, ok=ok, latency_ms=10.0, error=None if ok else "boom")


async def no_sleep(delay: float) -> None:
    pass


@pytest.fixture
def storage() -> Iterator[Storage]:
    with Storage(":memory:") as storage:
        yield storage


def make_monitor(
    storage: Storage,
    check: CheckFn,
    notifier: FakeNotifier,
    *,
    retries: int = 2,
    concurrency: int = 10,
    targets: list[DnsTarget] | None = None,
) -> Monitor:
    settings = Settings(targets=list(targets or [TARGET]), retries=retries, concurrency=concurrency)
    return Monitor(
        settings,
        check=check,
        storage=storage,
        metrics=Metrics(),
        notifier=notifier,
        sleep=no_sleep,
    )


async def test_transient_failure_is_absorbed_by_retry_and_never_alerts(storage: Storage) -> None:
    notifier = FakeNotifier()
    check = ScriptedCheck(False, True)
    result = await make_monitor(storage, check, notifier).check_target(TARGET)

    assert result.ok
    assert check.attempts == 2
    assert notifier.messages == []


async def test_alerts_once_when_down_and_once_when_recovered(storage: Storage) -> None:
    notifier = FakeNotifier()
    # retries=2 -> 3 attempts per cycle: two full failing cycles, then a success.
    check = ScriptedCheck(False, False, False, False, False, False, True)
    monitor = make_monitor(storage, check, notifier)

    for _ in range(2):
        assert not (await monitor.check_target(TARGET)).ok
    assert len(notifier.messages) == 1
    assert "DOWN" in notifier.messages[0]

    assert (await monitor.check_target(TARGET)).ok
    assert len(notifier.messages) == 2
    assert "back UP" in notifier.messages[1]

    assert (await monitor.check_target(TARGET)).ok
    assert len(notifier.messages) == 2


async def test_every_cycle_is_persisted_even_without_state_change(storage: Storage) -> None:
    monitor = make_monitor(storage, ScriptedCheck(True), FakeNotifier())
    for _ in range(3):
        await monitor.check_target(TARGET)

    assert len(storage.latest()) == 1
    count = storage._conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    assert count == 3


async def test_restart_does_not_realert_for_a_target_that_is_still_down(storage: Storage) -> None:
    first = FakeNotifier()
    await make_monitor(storage, ScriptedCheck(False), first, retries=0).check_target(TARGET)
    assert len(first.messages) == 1

    second = FakeNotifier()  # a "restarted" process reading the same database
    await make_monitor(storage, ScriptedCheck(False), second, retries=0).check_target(TARGET)
    assert second.messages == []


async def test_concurrency_is_limited_by_semaphore(storage: Storage) -> None:
    running = peak = 0

    async def check(target: Target) -> CheckResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return CheckResult(target.name, ok=True)

    targets = [DnsTarget(name=f"t{i}", host=f"t{i}.test") for i in range(10)]
    monitor = make_monitor(storage, check, FakeNotifier(), concurrency=3, targets=targets)
    await asyncio.gather(*(monitor.check_target(t) for t in targets))

    assert peak == 3


class TimedCheck:
    """Returns scripted (ok, checked_at) outcomes, one per attempt; the last one repeats."""

    def __init__(self, *outcomes: tuple[bool, datetime]) -> None:
        self.outcomes = outcomes
        self.attempts = 0

    async def __call__(self, target: Target) -> CheckResult:
        ok, at = self.outcomes[min(self.attempts, len(self.outcomes) - 1)]
        self.attempts += 1
        return CheckResult(target.name, ok=ok, checked_at=at, error=None if ok else "boom")


T0 = datetime(2030, 1, 1, tzinfo=UTC)


async def test_incident_is_opened_when_down_and_closed_when_recovered(storage: Storage) -> None:
    notifier = FakeNotifier()
    down_at, up_at = T0, T0 + timedelta(minutes=5, seconds=12)
    check = TimedCheck((False, down_at), (False, down_at + timedelta(minutes=1)), (True, up_at))
    monitor = make_monitor(storage, check, notifier, retries=0)

    await monitor.check_target(TARGET)
    [opened] = storage.incidents()
    assert (opened.started_at, opened.ended_at, opened.first_error) == (down_at, None, "boom")

    await monitor.check_target(TARGET)  # still down: same incident, no new row
    assert len(storage.incidents()) == 1

    await monitor.check_target(TARGET)
    [closed] = storage.incidents()
    assert closed.ended_at == up_at
    assert "Downtime: 5m 12s" in notifier.messages[-1]


async def test_incident_that_survived_a_restart_is_closed_on_recovery(storage: Storage) -> None:
    check_down = TimedCheck((False, T0))
    await make_monitor(storage, check_down, FakeNotifier(), retries=0).check_target(TARGET)

    notifier = FakeNotifier()  # a restarted monitor on the same database
    check_up = TimedCheck((True, T0 + timedelta(hours=2)))
    await make_monitor(storage, check_up, notifier, retries=0).check_target(TARGET)

    [incident] = storage.incidents()
    assert incident.ended_at == T0 + timedelta(hours=2)
    assert "Downtime: 2h 0m" in notifier.messages[0]


async def test_failure_absorbed_by_retry_leaves_no_incident(storage: Storage) -> None:
    await make_monitor(storage, ScriptedCheck(False, True), FakeNotifier()).check_target(TARGET)
    assert storage.incidents() == []


class LatencyCheck:
    """Succeeds with the scripted latency (None = failure); the last value repeats."""

    def __init__(self, *latencies: float | None) -> None:
        self.latencies = latencies
        self.attempts = 0

    async def __call__(self, target: Target) -> CheckResult:
        latency = self.latencies[min(self.attempts, len(self.latencies) - 1)]
        self.attempts += 1
        if latency is None:
            return CheckResult(target.name, ok=False, error="boom")
        return CheckResult(target.name, ok=True, latency_ms=latency)


def make_slow_aware_monitor(
    storage: Storage, check: CheckFn, notifier: FakeNotifier, targets: list[DnsTarget]
) -> Monitor:
    settings = Settings(targets=list(targets), retries=0, degraded_ms=500)
    return Monitor(
        settings, check=check, storage=storage, metrics=Metrics(), notifier=notifier, sleep=no_sleep
    )


async def test_slowdown_alerts_once_is_stored_and_is_not_an_incident(storage: Storage) -> None:
    notifier = FakeNotifier()
    check = LatencyCheck(100, 900, 950, 100)
    monitor = make_slow_aware_monitor(storage, check, notifier, [TARGET])

    results = [await monitor.check_target(TARGET) for _ in range(4)]

    assert [r.degraded for r in results] == [False, True, True, False]
    assert all(r.ok for r in results)
    assert len(notifier.messages) == 2
    assert "DEGRADED" in notifier.messages[0]
    assert "back to normal" in notifier.messages[1]
    assert storage.incidents() == []


async def test_a_degraded_target_that_dies_alerts_as_down(storage: Storage) -> None:
    notifier = FakeNotifier()
    monitor = make_slow_aware_monitor(storage, LatencyCheck(900, None), notifier, [TARGET])

    await monitor.check_target(TARGET)
    await monitor.check_target(TARGET)

    assert "DEGRADED" in notifier.messages[0]
    assert "DOWN" in notifier.messages[1]
    assert len(storage.incidents()) == 1


async def test_per_target_threshold_overrides_the_global_one(storage: Storage) -> None:
    tolerant = DnsTarget(name="tolerant", host="t.test", degraded_ms=2000)
    strict = DnsTarget(name="strict", host="s.test")
    monitor = make_slow_aware_monitor(
        storage, LatencyCheck(900), FakeNotifier(), [tolerant, strict]
    )

    assert not (await monitor.check_target(tolerant)).degraded  # 900 < 2000
    assert (await monitor.check_target(strict)).degraded  # 900 > global 500


async def test_restart_does_not_realert_for_a_target_that_is_still_slow(storage: Storage) -> None:
    first = FakeNotifier()
    monitor = make_slow_aware_monitor(storage, LatencyCheck(900), first, [TARGET])
    await monitor.check_target(TARGET)
    assert len(first.messages) == 1

    second = FakeNotifier()
    restarted = make_slow_aware_monitor(storage, LatencyCheck(900), second, [TARGET])
    await restarted.check_target(TARGET)

    assert second.messages == []


async def test_target_retries_override_the_global_retries(storage: Storage) -> None:
    patient = DnsTarget(name="patient", host="p.test", retries=4)
    impatient = DnsTarget(name="impatient", host="i.test", retries=0)
    settings = Settings(targets=[patient, impatient], retries=2)
    check = ScriptedCheck(False)
    monitor = Monitor(
        settings, check=check, storage=storage, metrics=Metrics(), notifier=None, sleep=no_sleep
    )

    await monitor.check_target(patient)
    assert check.attempts == 5  # 1 + 4 retries, not the global 1 + 2

    check.attempts = 0
    await monitor.check_target(impatient)
    assert check.attempts == 1


async def test_each_target_is_checked_on_its_own_interval(storage: Storage) -> None:
    class Stop(Exception):
        pass

    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise Stop

    fast = DnsTarget(name="fast", host="f.test", interval=5)
    settings = Settings(targets=[fast], interval=300, retries=0)
    monitor = Monitor(
        settings,
        check=ScriptedCheck(True),
        storage=storage,
        metrics=Metrics(),
        notifier=None,
        sleep=record_sleep,
    )

    with pytest.raises(Stop):
        await monitor._run_target(fast)

    assert sleeps == [pytest.approx(5, abs=1), pytest.approx(5, abs=1)]


def snapshot_of(board: StatusBoard) -> dict[str, Any]:
    [target] = board.snapshot()["targets"]
    return target  # type: ignore[no-any-return]


class _StopLoop(Exception):
    pass


async def run_loop(
    monitor: Monitor,
    target: DnsTarget,
    *,
    steps: int,
    on_sleep: Callable[[int], None] = lambda n: None,
) -> None:
    """Run the target's loop with instant sleeps; `on_sleep(n)` fires at the n-th sleep."""
    calls = 0

    async def fake_sleep(delay: float) -> None:
        nonlocal calls
        calls += 1
        on_sleep(calls)
        if calls >= steps:
            raise _StopLoop

    monitor._sleep = fake_sleep
    with contextlib.suppress(_StopLoop):
        await monitor._run_target(target)


def loop_monitor(
    storage: Storage,
    check: CheckFn,
    notifier: FakeNotifier,
    target: DnsTarget,
    *,
    board: StatusBoard | None = None,
    metrics: Metrics | None = None,
) -> Monitor:
    settings = Settings(targets=[target], retries=0, interval=30)
    return Monitor(
        settings,
        check=check,
        storage=storage,
        metrics=metrics or Metrics(),
        notifier=notifier,
        board=board,
        sleep=no_sleep,
        clock=lambda: T0 + timedelta(hours=1),
    )


async def test_pausing_stops_checks_and_alerts_and_closes_the_open_incident(
    storage: Storage,
) -> None:
    notifier = FakeNotifier()
    check = ScriptedCheck(False)
    monitor = loop_monitor(storage, check, notifier, TARGET)

    await run_loop(
        monitor,
        TARGET,
        steps=12,
        on_sleep=lambda n: storage.pause("svc", T0) if n == 2 else None,
    )

    assert check.attempts == 1  # the one check before the pause, nothing after it
    assert len(notifier.messages) == 1  # the DOWN alert; the pause itself alerts nobody
    [incident] = storage.incidents()
    assert incident.ended_at == T0 + timedelta(hours=1)


async def test_a_target_paused_in_the_config_is_never_checked(storage: Storage) -> None:
    target = DnsTarget(name="svc", host="svc.test", paused=True)
    check = ScriptedCheck(False)
    board = StatusBoard([target], interval=30)
    monitor = loop_monitor(storage, check, FakeNotifier(), target, board=board)

    await run_loop(monitor, target, steps=5)

    assert check.attempts == 0
    assert snapshot_of(board)["state"] == "paused"


async def test_resuming_judges_the_target_afresh(storage: Storage) -> None:
    notifier = FakeNotifier()
    monitor = loop_monitor(storage, ScriptedCheck(False), notifier, TARGET)

    def toggle(n: int) -> None:
        if n == 2:
            storage.pause("svc", T0)
        elif n == 6:
            storage.resume("svc")

    await run_loop(monitor, TARGET, steps=9, on_sleep=toggle)

    # Still down after maintenance: a new DOWN alert and a new incident, not a silent continuation.
    assert len(notifier.messages) == 2
    assert [i.ended_at is None for i in storage.incidents()] == [True, False]


async def test_coming_back_up_after_maintenance_sends_no_recovery_alert(storage: Storage) -> None:
    notifier = FakeNotifier()
    monitor = loop_monitor(storage, ScriptedCheck(False, True), notifier, TARGET)

    def toggle(n: int) -> None:
        if n == 2:
            storage.pause("svc", T0)
        elif n == 6:
            storage.resume("svc")

    await run_loop(monitor, TARGET, steps=9, on_sleep=toggle)

    assert len(notifier.messages) == 1  # only the original DOWN


async def test_a_paused_target_leaves_no_stale_metrics_and_shows_as_paused(
    storage: Storage,
) -> None:
    metrics = Metrics()
    board = StatusBoard([TARGET], interval=30)
    monitor = loop_monitor(
        storage, ScriptedCheck(True), FakeNotifier(), TARGET, board=board, metrics=metrics
    )

    await run_loop(
        monitor,
        TARGET,
        steps=6,
        on_sleep=lambda n: storage.pause("svc", T0) if n == 2 else None,
    )

    assert metrics.registry.get_sample_value("netpulse_up", {"target": "svc"}) is None
    snapshot = snapshot_of(board)
    assert snapshot["state"] == "paused"
    assert snapshot["since"] == (T0 + timedelta(hours=1)).isoformat()
