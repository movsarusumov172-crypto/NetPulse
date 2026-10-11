"""Отслеживание состояния up/down: превращает поток результатов в события смены состояния."""

from collections.abc import Mapping
from enum import StrEnum

from netpulse.models import CheckResult


class Event(StrEnum):
    WENT_DOWN = "went_down"
    RECOVERED = "recovered"


class StateTracker:
    """Помнит последнее известное состояние каждой цели и сообщает только об изменениях.

    У цели без известного состояния нет ни «up», ни «down»: первая успешная
    проверка проходит молча, а первая неудачная — событие, о котором нужно сообщить.
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
