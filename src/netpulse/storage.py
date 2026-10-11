"""Хранение результатов проверок в SQLite."""

import sqlite3
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from netpulse.models import CheckResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target        TEXT    NOT NULL,
    checked_at    TEXT    NOT NULL,
    ok            INTEGER NOT NULL,
    latency_ms    REAL,
    status_code   INTEGER,
    ssl_days_left INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_target_time ON results (target, checked_at);
"""


class Storage:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        # WAL позволяет `netpulse status` читать, пока монитор пишет.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

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
                " ssl_days_left, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result.target,
                    result.checked_at.isoformat(),
                    int(result.ok),
                    result.latency_ms,
                    result.status_code,
                    result.ssl_days_left,
                    result.error,
                ),
            )

    def latest(self) -> list[CheckResult]:
        """Последний результат каждой цели, отсортированный по имени цели."""
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
            )
            for row in rows
        ]

    def last_states(self) -> dict[str, bool]:
        return {result.target: result.ok for result in self.latest()}

    def uptime_ratios(self, since: datetime) -> dict[str, float]:
        """Доля успешных проверок по каждой цели начиная с указанного момента (UTC)."""
        rows = self._conn.execute(
            "SELECT target, AVG(ok) AS ratio FROM results WHERE checked_at >= ? GROUP BY target",
            (since.isoformat(),),
        )
        return {row["target"]: row["ratio"] for row in rows}
