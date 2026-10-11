"""Target state tracking: turns a stream of results into state-change events."""

from collections.abc import Collection, Mapping
from dataclasses import replace
from enum import StrEnum

from netpulse.models import CheckResult


class Event(StrEnum):
    WENT_DOWN = "went_down"
    RECOVERED = "recovered"
    DEGRADED = "degraded"
    NORMALIZED = "normalized"


def classify(result: CheckResult, threshold_ms: float | None) -> CheckResult:
    """Mark a successful result as degraded when it is slower than the latency threshold.

    Degraded is not down: the target still answers, so `ok` stays True.
    """
    if (
        threshold_ms is None
        or not result.ok
        or result.latency_ms is None
        or result.latency_ms <= threshold_ms
    ):
        return result
    return replace(result, degraded=True)


class StateTracker:
    """Remembers the last known state per target and reports only changes.

    A target with no known state is not "up" or "down" yet: its first successful
    check is silent, but a first failed check is an event worth alerting on. The same goes
    for degradation: a slow answer is reported once when it starts and once when it ends.
    """

    def __init__(
        self, initial: Mapping[str, bool] | None = None, degraded: Collection[str] = ()
    ) -> None:
        self._is_up: dict[str, bool] = dict(initial or {})
        self._degraded: set[str] = set(degraded)

    def update(self, result: CheckResult) -> Event | None:
        name = result.target
        previous = self._is_up.get(name)
        was_degraded = name in self._degraded
        degraded = result.ok and result.degraded
        self._is_up[name] = result.ok
        self._degraded.discard(name)
        if degraded:
            self._degraded.add(name)

        if not result.ok:
            return None if previous is False else Event.WENT_DOWN
        if previous is False:
            return Event.RECOVERED
        if degraded and not was_degraded:
            return Event.DEGRADED
        if was_degraded and not degraded:
            return Event.NORMALIZED
        return None
