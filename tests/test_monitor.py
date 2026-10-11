import asyncio
from collections.abc import Iterator

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
    """Возвращает по одному заданному исходу на попытку; последний повторяется."""

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
    # retries=2 -> 3 попытки за цикл: два полностью неудачных цикла, затем успех.
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

    second = FakeNotifier()  # «перезапущенный» процесс читает ту же базу
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
