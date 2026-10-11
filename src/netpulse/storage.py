"""SQLite persistence for check results."""

import sqlite3
from collections.abc import Callable, Collection
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

from netpulse.models import CheckResult, Incident, LatencyBucket, Uptime


class StorageError(Exception):
    """The database cannot be used by this version of NetPulse."""


def _migration_1_results(conn: sqlite3.Connection) -> None:
    """The original schema. Idempotent, so databases created before versioning still match."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS results ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " target TEXT NOT NULL,"
        " checked_at TEXT NOT NULL,"
        " ok INTEGER NOT NULL,"
        " latency_ms REAL,"
        " status_code INTEGER,"
        " ssl_days_left INTEGER,"
        " error TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_results_target_time ON results (target, checked_at)"
    )


def _migration_2_incidents(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE incidents ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " target TEXT NOT NULL,"
        " started_at TEXT NOT NULL,"
        " ended_at TEXT,"
        " first_error TEXT)"
    )
    conn.execute("CREATE INDEX idx_incidents_target_started ON incidents (target, started_at)")
    conn.execute("CREATE INDEX idx_incidents_started ON incidents (started_at)")
    _backfill_incidents(conn)


def _migration_3_degraded(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE results ADD COLUMN degraded INTEGER NOT NULL DEFAULT 0")


def _migration_4_maintenance(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE paused_targets (target TEXT PRIMARY KEY, paused_at TEXT NOT NULL)")


def _backfill_incidents(conn: sqlite3.Connection) -> None:
    """Rebuild past incidents from stored results so existing history shows up right away.

    An incident is a run of failed results of one target: it starts at the first failure and
    ends at the next success. A run that is still going at the end of the data stays open.
    """
    open_runs: dict[str, tuple[str, str | None]] = {}
    rows: list[tuple[str, str, str | None, str | None]] = []
    for target, checked_at, ok, error in conn.execute(
        "SELECT target, checked_at, ok, error FROM results ORDER BY id"
    ):
        if not ok:
            open_runs.setdefault(target, (checked_at, error))
        elif target in open_runs:
            started_at, first_error = open_runs.pop(target)
            rows.append((target, started_at, checked_at, first_error))
    rows.extend((target, start, None, error) for target, (start, error) in open_runs.items())
    conn.executemany(
        "INSERT INTO incidents (target, started_at, ended_at, first_error) VALUES (?, ?, ?, ?)",
        rows,
    )


# Append-only: the position in this tuple is the schema version (`PRAGMA user_version`).
_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_1_results,
    _migration_2_incidents,
    _migration_3_degraded,
    _migration_4_maintenance,
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the database up to the latest schema in one transaction."""
    conn.execute("BEGIN IMMEDIATE")  # also serialises two processes opening the file at once
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > len(_MIGRATIONS):
            raise StorageError(
                f"database schema version {version} is newer than this NetPulse supports"
                f" ({len(_MIGRATIONS)}); upgrade NetPulse"
            )
        for migration in _MIGRATIONS[version:]:
            migration(conn)
        conn.execute(f"PRAGMA user_version = {len(_MIGRATIONS)}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class Storage:
    """One connection, usable from the thread that created it only.

    Other threads (the HTTP server) open their own `readonly=True` instance.
    """

    def __init__(self, path: Path | str, *, readonly: bool = False) -> None:
        if readonly:
            self._conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
            return
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        # WAL lets `netpulse status` read while the monitor is writing.
        self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            _migrate(self._conn)
        except BaseException:
            self._conn.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def save(self, result: CheckResult) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO results (target, checked_at, ok, latency_ms, status_code,"
                " ssl_days_left, error, degraded) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.target,
                    result.checked_at.isoformat(),
                    int(result.ok),
                    result.latency_ms,
                    result.status_code,
                    result.ssl_days_left,
                    result.error,
                    int(result.degraded),
                ),
            )

    def latest(self) -> list[CheckResult]:
        """The most recent result of every target, ordered by target name."""
        rows = self._conn.execute(
            "SELECT r.* FROM results r"
            " JOIN (SELECT MAX(id) AS id FROM results GROUP BY target) last ON r.id = last.id"
            " ORDER BY r.target"
        )
        return [
            CheckResult(
                target=row["target"],
                ok=bool(row["ok"]),
                latency_ms=row["latency_ms"],
                status_code=row["status_code"],
                ssl_days_left=row["ssl_days_left"],
                error=row["error"],
                checked_at=datetime.fromisoformat(row["checked_at"]),
                degraded=bool(row["degraded"]),
            )
            for row in rows
        ]

    def last_states(self) -> dict[str, bool]:
        return {result.target: result.ok for result in self.latest()}

    def last_degraded(self) -> set[str]:
        """Targets whose most recent result was a successful but slow one."""
        return {result.target for result in self.latest() if result.degraded}

    def uptime_ratios(self, since: datetime) -> dict[str, float]:
        """Share of successful checks per target since the given moment (UTC)."""
        rows = self._conn.execute(
            "SELECT target, AVG(ok) AS ratio FROM results WHERE checked_at >= ? GROUP BY target",
            (since.isoformat(),),
        )
        return {row["target"]: row["ratio"] for row in rows}

    def uptime(self, now: datetime) -> dict[str, Uptime]:
        """24-hour and 7-day uptime per target, computed from the stored results.

        Uptime is the share of successful checks, so a stretch in which the monitor itself
        was not running (and therefore stored nothing) neither raises nor lowers it.
        """
        day = self.uptime_ratios(now - timedelta(hours=24))
        week = self.uptime_ratios(now - timedelta(days=7))
        return {target: Uptime(day.get(target), ratio) for target, ratio in week.items()}

    def latency_series(
        self, target: str, since: datetime, bucket_seconds: int = 300
    ) -> list[LatencyBucket]:
        """Latency of one target since `since`, averaged into fixed slots (oldest first).

        Slots without any check are omitted, so gaps (monitor off, target paused) stay visible.
        """
        slots: dict[int, list[float]] = {}  # slot start -> [checks, failures, sum, n, max]
        rows = self._conn.execute(
            "SELECT checked_at, ok, latency_ms FROM results"
            " WHERE target = ? AND checked_at >= ? ORDER BY checked_at",
            (target, since.isoformat()),
        )
        for row in rows:
            moment = int(datetime.fromisoformat(row["checked_at"]).timestamp())
            slot = slots.setdefault(moment - moment % bucket_seconds, [0, 0, 0.0, 0, 0.0])
            slot[0] += 1
            slot[1] += 0 if row["ok"] else 1
            if row["latency_ms"] is not None:
                slot[2] += row["latency_ms"]
                slot[3] += 1
                slot[4] = max(slot[4], row["latency_ms"])
        return [
            LatencyBucket(
                start=datetime.fromtimestamp(start, UTC),
                checks=int(checks),
                failures=int(failures),
                avg_ms=total / measured if measured else None,
                max_ms=peak if measured else None,
            )
            for start, (checks, failures, total, measured, peak) in sorted(slots.items())
        ]

    def open_incident(self, target: str, started_at: datetime, error: str | None) -> None:
        """Start an incident unless the target already has an open one."""
        with self._conn:
            already_open = self._conn.execute(
                "SELECT 1 FROM incidents WHERE target = ? AND ended_at IS NULL", (target,)
            ).fetchone()
            if already_open is None:
                self._conn.execute(
                    "INSERT INTO incidents (target, started_at, first_error) VALUES (?, ?, ?)",
                    (target, started_at.isoformat(), error),
                )

    def close_incident(self, target: str, ended_at: datetime) -> Incident | None:
        """End the target's open incident. Returns it, or None if there was none."""
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE target = ? AND ended_at IS NULL"
                " ORDER BY id DESC LIMIT 1",
                (target,),
            ).fetchone()
            if row is None:
                return None
            incident = _incident_from_row(row)
            ended_at = max(ended_at, incident.started_at)
            self._conn.execute(
                "UPDATE incidents SET ended_at = ? WHERE id = ?", (ended_at.isoformat(), row["id"])
            )
        return replace(incident, ended_at=ended_at)

    def incidents(
        self, *, limit: int = 50, targets: Collection[str] | None = None
    ) -> list[Incident]:
        """Most recent incidents first, optionally only for the given targets."""
        query = "SELECT * FROM incidents"
        params: list[object] = []
        if targets is not None:
            query += f" WHERE target IN ({', '.join('?' * len(targets))})"
            params.extend(targets)
        query += " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [_incident_from_row(row) for row in self._conn.execute(query, params)]

    def pause(self, target: str, paused_at: datetime) -> bool:
        """Put a target into maintenance. Returns False if it already was."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO paused_targets (target, paused_at) VALUES (?, ?)",
                (target, paused_at.isoformat()),
            )
        return cursor.rowcount > 0

    def resume(self, target: str) -> bool:
        """End a target's maintenance. Returns False if it was not paused."""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM paused_targets WHERE target = ?", (target,))
        return cursor.rowcount > 0

    def paused_names(self) -> set[str]:
        return {row["target"] for row in self._conn.execute("SELECT target FROM paused_targets")}


def _incident_from_row(row: sqlite3.Row) -> Incident:
    ended_at = row["ended_at"]
    return Incident(
        id=row["id"],
        target=row["target"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(ended_at) if ended_at else None,
        first_error=row["first_error"],
    )
