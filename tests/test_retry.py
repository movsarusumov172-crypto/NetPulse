from netpulse.models import CheckResult
from netpulse.retry import check_with_retry


class Script:
    """An attempt callable that replays a fixed list of outcomes and records sleeps."""

    def __init__(self, *outcomes: bool) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.delays: list[float] = []

    async def attempt(self) -> CheckResult:
        ok = self.outcomes[self.calls]
        self.calls += 1
        return CheckResult("svc", ok=ok, error=None if ok else f"fail #{self.calls}")

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


async def run(script: Script, retries: int = 3, base_delay: float = 1.0) -> CheckResult:
    return await check_with_retry(
        script.attempt, retries=retries, base_delay=base_delay, sleep=script.sleep
    )


async def test_success_on_first_attempt_does_not_retry() -> None:
    script = Script(True)
    result = await run(script)
    assert result.ok
    assert script.calls == 1
    assert script.delays == []


async def test_delay_doubles_between_retries() -> None:
    script = Script(False, False, False, True)
    result = await run(script, base_delay=0.5)
    assert result.ok
    assert script.calls == 4
    assert script.delays == [0.5, 1.0, 2.0]


async def test_gives_up_after_all_retries_and_returns_last_failure() -> None:
    script = Script(False, False, False, False)
    result = await run(script, retries=3)
    assert not result.ok
    assert result.error == "fail #4"
    assert script.calls == 4
    assert script.delays == [1.0, 2.0, 4.0]


async def test_zero_retries_means_a_single_attempt() -> None:
    script = Script(False)
    result = await run(script, retries=0)
    assert not result.ok
    assert script.calls == 1
    assert script.delays == []
