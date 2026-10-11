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


T0 = datetime(2030, 1, 1, tzinfo=UTC)


def test_incident_lifecycle_open_then_close() -> None:
    with Storage(":memory:") as storage:
        storage.open_incident("web", T0, "ConnectError: refused")
        [ongoing] = storage.incidents()
        assert ongoing.ended_at is None
        assert ongoing.first_error == "ConnectError: refused"

        closed = storage.close_incident("web", T0 + timedelta(seconds=90))

        assert closed is not None
        assert closed.duration(T0 + timedelta(days=1)) == timedelta(seconds=90)
        [stored] = storage.incidents()
        assert stored.ended_at == T0 + timedelta(seconds=90)


def test_opening_twice_does_not_duplicate_an_open_incident() -> None:
    with Storage(":memory:") as storage:
        storage.open_incident("web", T0, "first")
        storage.open_incident("web", T0 + timedelta(minutes=1), "second")

        [incident] = storage.incidents()

        assert incident.first_error == "first"
        assert incident.started_at == T0


def test_closing_without_an_open_incident_returns_none() -> None:
    with Storage(":memory:") as storage:
        assert storage.close_incident("web", T0) is None
        storage.open_incident("web", T0, None)
        storage.close_incident("web", T0 + timedelta(minutes=1))
        assert storage.close_incident("web", T0 + timedelta(minutes=2)) is None


def test_a_target_can_have_several_incidents_and_a_reopened_one_is_new() -> None:
    with Storage(":memory:") as storage:
        storage.open_incident("web", T0, "a")
        storage.close_incident("web", T0 + timedelta(minutes=1))
        storage.open_incident("web", T0 + timedelta(minutes=10), "b")

        incidents = storage.incidents()

        assert [i.first_error for i in incidents] == ["b", "a"]  # newest first
        assert [i.ended_at is None for i in incidents] == [True, False]


def test_closing_before_the_start_never_gives_a_negative_duration() -> None:
    with Storage(":memory:") as storage:
        storage.open_incident("web", T0, None)
        closed = storage.close_incident("web", T0 - timedelta(seconds=5))

        assert closed is not None
        assert closed.duration(T0) == timedelta(0)


def test_incidents_can_be_limited_and_filtered_by_target() -> None:
    with Storage(":memory:") as storage:
        for minute, target in enumerate(["a", "b", "a", "c"]):
            storage.open_incident(target, T0 + timedelta(minutes=minute), None)
            storage.close_incident(target, T0 + timedelta(minutes=minute, seconds=30))

        assert [i.target for i in storage.incidents(limit=2)] == ["c", "a"]
        assert [i.target for i in storage.incidents(targets=["a", "b"])] == ["a", "b", "a"]
        assert storage.incidents(targets=[]) == []


def test_migration_rebuilds_past_incidents_from_stored_results(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, checked_at TEXT NOT NULL,
            ok INTEGER NOT NULL, latency_ms REAL, status_code INTEGER, ssl_days_left INTEGER,
            error TEXT
        );
        INSERT INTO results (target, checked_at, ok, error) VALUES
            ('web', '2030-01-01T00:00:00+00:00', 1, NULL),
            ('web', '2030-01-01T00:01:00+00:00', 0, 'first failure'),
            ('db',  '2030-01-01T00:01:30+00:00', 1, NULL),
            ('web', '2030-01-01T00:02:00+00:00', 0, 'second failure'),
            ('web', '2030-01-01T00:03:00+00:00', 1, NULL),
            ('web', '2030-01-01T00:04:00+00:00', 0, 'still down');
        """
    )
    conn.commit()
    conn.close()

    with Storage(path) as storage:
        incidents = {i.first_error: i for i in storage.incidents()}

    assert set(incidents) == {"first failure", "still down"}
    closed = incidents["first failure"]
    assert closed.target == "web"
    assert closed.started_at == datetime(2030, 1, 1, 0, 1, tzinfo=UTC)
    assert closed.ended_at == datetime(2030, 1, 1, 0, 3, tzinfo=UTC)
    assert incidents["still down"].ended_at is None
