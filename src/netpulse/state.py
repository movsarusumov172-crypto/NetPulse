"""Up/down state tracking: turns a stream of results into state-change events."""

from collections.abc import Mapping
from enum import StrEnum

from netpulse.models import CheckResult


class Event(StrEnum):
    WENT_DOWN = "went_down"
    RECOVERED = "recovered"


class StateTracker:
    """Remembers the last known state per target and reports only changes.

    A target with no known state is not "up" or "down" yet: its first successful
    check is silent, but a first failed check is an event worth alerting on.
    """

    def __init__(self, initial: Mapping[str, bool] | None = None) -> None:
        self._is_up: dict[str, bool] = dict(initial or {})

    def update(self, result: CheckResult) -> Event | None:
        previous = self._is_up.get(result.target)
        self._is_up[result.target] = result.ok
        if previous == result.ok:
            return None
        if not result.ok:
            return Event.WENT_DOWN
        return Event.RECOVERED if previous is False else None
