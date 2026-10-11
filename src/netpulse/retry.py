"""Retry with exponential backoff."""

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
    """Run `attempt` until it succeeds or `retries` extra attempts are used up.

    The delay before retry N (0-based) is `base_delay * 2**N`. Returns the first
    successful result, or the last failure.
    """
    result = await attempt()
    for retry in range(retries):
        if result.ok:
            break
        await sleep(base_delay * 2**retry)
        result = await attempt()
    return result
