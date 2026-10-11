import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from netpulse.config import Settings
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
