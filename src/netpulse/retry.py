"""Повторные попытки с экспоненциальной задержкой."""

import asyncio
from collections.abc import Awaitable, Callable

from netpulse.models import CheckResult


async def check_with_retry(
    attempt: Callable[[], Awaitable[CheckResult]],
    *,
    retries: int,
    base_delay: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CheckResult:
    """Выполняет `attempt`, пока он не удастся или не кончатся `retries` дополнительных попыток.

    Пауза перед повтором N (с нуля) равна `base_delay * 2**N`. Возвращает первый
    успешный результат или последний неудачный.
    """
    result = await attempt()
    for retry in range(retries):
        if result.ok:
            break
        await sleep(base_delay * 2**retry)
        result = await attempt()
    return result
