"""The monitoring loop: schedules checks and reacts to their results."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx

from netpulse.checks import Checker
from netpulse.config import ConfigError, Settings, load_settings
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
PAUSE_POLL_SECONDS = 5.0  # how soon a pause/resume is noticed, whatever the check interval
CONFIG_POLL_SECONDS = 2.0  # how often the config file is checked for edits
# Read once at startup (sockets, database, semaphore size): changing them needs a restart.
_RESTART_ONLY = ("database", "metrics_host", "metrics_port", "concurrency")


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        config_path: Path | None = None,
        on_settings: Callable[[Settings], None] | None = None,
    ) -> None:
        self._settings = settings
        self._check = check
        self._storage = storage
        self._metrics = metrics
        self._notifier = notifier
        self._board = board
        self._sleep = sleep
        self._clock = clock
        self._config_path = config_path
        self._on_settings = on_settings
        self._config_signature = self._file_signature()
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        # Seed from the database so a restart does not re-alert about a target that is still down.
        self._tracker = StateTracker(storage.last_states(), storage.last_degraded())
        self._group: asyncio.TaskGroup | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running: dict[str, Target] = {}  # the target spec each running task was started with

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            self._group = group
            self._sync_tasks()
            if self._config_path is not None:
                group.create_task(self._watch_config(), name="config-watcher")

    # --- scheduling ----------------------------------------------------------------------------

    def _sync_tasks(self) -> None:
        """Make the running per-target loops match the current settings."""
        if self._group is None:
            return
        wanted = {target.name: target for target in self._settings.targets}
        for name in list(self._tasks):
            if name not in wanted:
                self._stop_target(name)
                self._forget_removed_target(name)
            elif self._running[name] != wanted[name]:
                self._stop_target(name)  # restarted below with the new spec
        for name, target in wanted.items():
            if name not in self._tasks:
                self._tasks[name] = self._group.create_task(
                    self._run_target(target), name=f"target:{name}"
                )
                self._running[name] = target

    def _stop_target(self, name: str) -> None:
        self._tasks.pop(name).cancel()
        del self._running[name]

    def _forget_removed_target(self, name: str) -> None:
        self._tracker.forget(name)
        self._storage.close_incident(name, self._clock())
        self._metrics.forget(name)
        logger.info("%s was removed from the config and is no longer monitored", name)

    async def _run_target(self, target: Target) -> None:
        paused = False
        if self._board is not None:
            self._board.set_paused(target.name, False)  # a restarted loop starts from a clean slate
        while True:
            if self._is_paused(target) != paused:
                paused = not paused
                self._on_pause_changed(target, paused)
            if paused:
                await self._sleep(PAUSE_POLL_SECONDS)
                continue
            started = time.monotonic()
            await self.check_target(target)
            interval = self._settings.interval_for(target)
            await self._wait(max(0.0, interval - (time.monotonic() - started)), target)

    def _is_paused(self, target: Target) -> bool:
        """Paused by `paused: true` in the config or by `netpulse pause` (stored in the DB)."""
        return target.paused or target.name in self._storage.paused_names()

    async def _wait(self, seconds: float, target: Target) -> None:
        """Sleep until the next check, in slices so a pause starts promptly."""
        remaining = seconds
        while remaining > 0:
            step = min(remaining, PAUSE_POLL_SECONDS)
            await self._sleep(step)
            remaining -= step
            if self._is_paused(target):
                return

    def _on_pause_changed(self, target: Target, paused: bool) -> None:
        if paused:
            # Maintenance is planned: end any open incident and forget the state, so the first
            # check after resuming is judged afresh and nothing is alerted about the pause itself.
            now = self._clock()
            self._tracker.forget(target.name)
            self._storage.close_incident(target.name, now)
            self._metrics.forget(target.name)
            logger.info("%s is in maintenance: checks and alerts are paused", target.name)
        else:
            logger.info("%s is out of maintenance: checks resume", target.name)
        if self._board is not None:
            self._board.set_paused(target.name, paused, self._clock())

    # --- config reload -------------------------------------------------------------------------

    async def _watch_config(self) -> None:
        while True:
            await self._sleep(CONFIG_POLL_SECONDS)
            self.reload_if_changed()

    def _file_signature(self) -> tuple[int, int] | None:
        if self._config_path is None:
            return None
        try:
            stat = self._config_path.stat()
        except OSError:  # e.g. an editor is replacing the file right now
            return None
        return stat.st_mtime_ns, stat.st_size

    def reload_if_changed(self) -> bool:
        """Re-read the config if the file changed. A broken file leaves the old config running."""
        signature = self._file_signature()
        if self._config_path is None or signature is None or signature == self._config_signature:
            return False
        self._config_signature = signature
        try:
            new = load_settings(self._config_path)
        except ConfigError as exc:
            logger.error("Config not reloaded, keeping the previous one: %s", exc)
            if self._board is not None:
                self._board.set_config_error(str(exc))
            return False
        if self._board is not None:
            self._board.set_config_error(None)
        self.apply_settings(new)
        logger.info("Config reloaded: %d targets", len(new.targets))
        return True

    def apply_settings(self, new: Settings) -> None:
        """Switch to new settings: start loops for added targets, stop those of removed ones."""
        kept = {}
        for name in _RESTART_ONLY:
            if getattr(new, name) != getattr(self._settings, name):
                logger.warning("Changing `%s` needs a restart; keeping the current value", name)
                kept[name] = getattr(self._settings, name)
        self._settings = new.model_copy(update=kept)
        self._sync_tasks()
        if self._board is not None:
            self._board.set_targets(list(new.targets), new.interval, degraded_ms=new.degraded_ms)
        if self._on_settings is not None:
            self._on_settings(self._settings)

    # --- one check -----------------------------------------------------------------------------

    async def check_target(self, target: Target) -> CheckResult:
        """One full check cycle: attempts with retries, then persist, expose and alert."""
        result = await check_with_retry(
            partial(self._attempt, target),
            retries=self._settings.retries_for(target),
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


async def run_monitor(settings: Settings, config_path: Path | None = None) -> None:
    """Wire up real dependencies and monitor until cancelled.

    With `config_path`, edits to that file are picked up without a restart.
    """
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
                    config_path=config_path,
                    on_settings=lambda new: checker.configure(
                        new.timeout, new.ssl_expiry_warn_days
                    ),
                )
                logger.info(
                    "Monitoring %d targets every %gs", len(settings.targets), settings.interval
                )
                await monitor.run()
        finally:
            server.stop()
