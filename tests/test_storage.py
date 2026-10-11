import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from netpulse.models import CheckResult
from netpulse.storage import _MIGRATIONS, Storage, StorageError


def test_latest_returns_most_recent_result_per_target() -> None:
    now = datetime.now(UTC)
    with Storage(":memory:") as storage:
        storage.save(CheckResult("a", ok=False, error="old", checked_at=now - timedelta(minutes=2)))
        storage.save(
            CheckResult(
                "a", ok=True, latency_ms=5.0, status_code=200, ssl_days_left=40, checked_at=now
            )
        )
        storage.save(CheckResult("b", ok=False, error="down", checked_at=now))

        latest = {r.target: r for r in storage.latest()}

    assert latest["a"].ok
    assert latest["a"].latency_ms == 5.0
    assert latest["a"].status_code == 200
    assert latest["a"].ssl_days_left == 40
    assert latest["a"].checked_at == now
    assert not latest["b"].ok
    assert latest["b"].error == "down"


def test_last_states_restores_up_down_flags() -> None:
    now = datetime.now(UTC)
    with Storage(":memory:") as storage:
        storage.save(CheckResult("a", ok=True, checked_at=now))
        storage.save(CheckResult("b", ok=False, checked_at=now))
        assert storage.last_states() == {"a": True, "b": False}


def test_uptime_ratio_only_counts_results_since_cutoff() -> None:
    now = datetime.now(UTC)
    with Storage(":memory:") as storage:
        storage.save(
            CheckResult("a", ok=False, checked_at=now - timedelta(days=2))
        )  # outside the window
        for ok in (True, True, True, False):
            storage.save(CheckResult("a", ok=ok, checked_at=now - timedelta(minutes=1)))
        assert storage.uptime_ratios(now - timedelta(hours=24)) == {"a": 0.75}


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _create_unversioned_database(path: Path) -> None:
    """The schema and data of a database created before migrations existed."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, checked_at TEXT NOT NULL,
            ok INTEGER NOT NULL, latency_ms REAL, status_code INTEGER, ssl_days_left INTEGER,
            error TEXT
        );
        CREATE INDEX idx_results_target_time ON results (target, checked_at);
        INSERT INTO results (target, checked_at, ok, latency_ms, error)
        VALUES ('web', '2030-01-01T00:00:00+00:00', 1, 12.5, NULL);
        """
    )
    conn.commit()
    conn.close()


def test_existing_database_without_version_is_migrated_and_keeps_its_data(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    _create_unversioned_database(path)

    with Storage(path) as storage:
        [result] = storage.latest()

    assert (result.target, result.ok, result.latency_ms) == ("web", True, 12.5)
    assert _user_version(path) == len(_MIGRATIONS)


def test_reopening_a_migrated_database_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "np.db"
    with Storage(path) as storage:
        storage.save(CheckResult("a", ok=True))
    with Storage(path) as storage:
        assert [r.target for r in storage.latest()] == ["a"]
    assert _user_version(path) == len(_MIGRATIONS)


def test_database_from_a_newer_version_is_refused_and_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(_MIGRATIONS) + 1}")
    conn.close()

    with pytest.raises(StorageError, match="newer"):
        Storage(path)

    assert _user_version(path) == len(_MIGRATIONS) + 1
