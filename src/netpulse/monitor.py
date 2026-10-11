"""The monitoring loop: schedules checks and reacts to their results."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial

import httpx

from netpulse.checks import Checker
from netpulse.config import Settings
from netpulse.dashboard import StatusBoard, StatusServer
from netpulse.metrics import Metrics
from netpulse.models import CheckResult, Target
from netpulse.notifier import Notifier, format_alert, telegram_from_env
from netpulse.retry import check_with_retry
from netpulse.state import Event, StateTracker, classify
from netpulse.storage import Storage

logger = logging.getLogger(__name__)

CheckFn = Callable[[Target], Awaitable[CheckResult]]
SleepFn = Callable[[float], Awaitable[None]]

_ALERT_TIMEOUT = 10.0


class Monitor:
    def __init__(
        self,
        settings: Settings,
        *,
        check: CheckFn,
        storage: Storage,
        metrics: Metrics,
        notifier: Notifier | None = None,
        board: StatusBoard | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._check = check
        self._storage = storage
        self._metrics = metrics
        self._notifier = notifier
        self._board = board
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        # Seed from the database so a restart does not re-alert about a target that is still down.
        self._tracker = StateTracker(storage.last_states(), storage.last_degraded())

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            for target in self._settings.targets:
                group.create_task(self._run_target(target))

    async def _run_target(self, target: Target) -> None:
        while True:
            started = time.monotonic()
            await self.check_target(target)
            await self._sleep(max(0.0, self._settings.interval - (time.monotonic() - started)))

    async def check_target(self, target: Target) -> CheckResult:
        """One full check cycle: attempts with retries, then persist, expose and alert."""
        result = await check_with_retry(
            partial(self._attempt, target),
            retries=self._settings.retries,
            base_delay=self._settings.retry_base_delay,
            sleep=self._sleep,
        )
        threshold_ms = self._settings.degraded_ms_for(target)
        result = classify(result, threshold_ms)
        logger.debug(
            "%s: ok=%s latency=%s error=%s", target.name, result.ok, result.latency_ms, result.error
        )
        self._storage.save(result)
        self._metrics.record(result)
        if self._board is not None:
            self._board.record(result)
        event = self._tracker.update(result)
        if event is not None:
            downtime = self._record_incident(target, result, event)
            self._log_event(target, result, event)
            if self._notifier is not None:
                alert = format_alert(
                    target, result, event, downtime=downtime, threshold_ms=threshold_ms
                )
                await self._notifier.send(alert)
        return result

    def _record_incident(
        self, target: Target, result: CheckResult, event: Event
    ) -> timedelta | None:
        """Open or close the target's incident. Returns the downtime when one just ended."""
        if event is Event.WENT_DOWN:
            self._storage.open_incident(target.name, result.checked_at, result.error)
        elif event is Event.RECOVERED:
            incident = self._storage.close_incident(target.name, result.checked_at)
            if incident is not None:
                return incident.duration(result.checked_at)
        return None

    async def _attempt(self, target: Target) -> CheckResult:
        # The slot is held per attempt, not per cycle, so retry backoff never blocks other checks.
        async with self._semaphore:
            return await self._check(target)

    @staticmethod
    def _log_event(target: Target, result: CheckResult, event: Event) -> None:
        if event is Event.WENT_DOWN:
            logger.warning("%s is DOWN: %s", target.name, result.error)
        elif event is Event.DEGRADED:
            logger.warning("%s is DEGRADED: latency %.0f ms", target.name, result.latency_ms or 0)
        elif event is Event.NORMALIZED:
            logger.info("%s latency is back to normal", target.name)
        else:
            logger.info("%s is back UP", target.name)


async def run_monitor(settings: Settings) -> None:
    """Wire up real dependencies and monitor until cancelled."""
    metrics = Metrics()
    board = StatusBoard(list(settings.targets), settings.interval, degraded_ms=settings.degraded_ms)
    with Storage(settings.database) as storage:  # opens (and migrates) the database first
        server = StatusServer(metrics.registry, board, settings.database)
        port = server.start(settings.metrics_host, settings.metrics_port)
        logger.info("Status page on http://%s:%s/ (metrics: /metrics)", settings.metrics_host, port)
        try:
            async with (
                httpx.AsyncClient(follow_redirects=True, timeout=None) as check_client,
                httpx.AsyncClient(timeout=_ALERT_TIMEOUT) as alert_client,
            ):
                checker = Checker(check_client, settings.timeout, settings.ssl_expiry_warn_days)
                monitor = Monitor(
                    settings,
                    check=checker.check,
                    storage=storage,
                    metrics=metrics,
                    notifier=telegram_from_env(alert_client),
                    board=board,
                )
                logger.info(
                    "Monitoring %d targets every %gs", len(settings.targets), settings.interval
                )
                await monitor.run()
        finally:
            server.stop()
