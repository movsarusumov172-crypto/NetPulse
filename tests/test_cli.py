from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from netpulse import cli
from netpulse.models import CheckResult, Target
from netpulse.storage import Storage

runner = CliRunner()


def write_config(tmp_path: Path, db: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"database: '{db.as_posix()}'\ntargets: [{{name: web, type: dns, host: a.test}}]\n",
        encoding="utf-8",
    )
    return config


def test_check_exit_code_reflects_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_check(target: Target, timeout: float, ssl_warn_days: int) -> CheckResult:
        return CheckResult(target.name, ok=False, status_code=500, latency_ms=20.0, error="bad")

    monkeypatch.setattr(cli, "run_check", fake_run_check)
    result = runner.invoke(cli.app, ["check", "https://x.test"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "status=500" in result.output


def test_status_lists_configured_targets_only(tmp_path: Path) -> None:
    db = tmp_path / "np.db"
    now = datetime.now(UTC)
    with Storage(db) as storage:
        storage.save(CheckResult("web", ok=False, error="timed out", checked_at=now))
        storage.save(CheckResult("removed-target", ok=True, checked_at=now))

    result = runner.invoke(cli.app, ["status", "--config", str(write_config(tmp_path, db))])

    assert result.exit_code == 0
    assert "web" in result.output
    assert "DOWN" in result.output
    assert "timed out" in result.output
    assert "removed-target" not in result.output


def test_status_without_database_fails_cleanly(tmp_path: Path) -> None:
    config = write_config(tmp_path, tmp_path / "missing.db")
    result = runner.invoke(cli.app, ["status", "--config", str(config)])
    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(None, "-"), (1.0, "100%"), (0.5, "50.00%"), (0.99996, "99.99%"), (0.0, "0.00%")],
)
def test_percent_rounds_down_so_an_imperfect_target_never_shows_100(
    ratio: float | None, expected: str
) -> None:
    assert cli._percent(ratio) == expected


def test_status_shows_both_uptime_windows_and_survives_old_results(tmp_path: Path) -> None:
    db = tmp_path / "np.db"
    now = datetime.now(UTC)
    with Storage(db) as storage:
        for ok in (True, True, True, False):
            storage.save(CheckResult("web", ok=ok, checked_at=now - timedelta(minutes=5)))
        storage.save(CheckResult("web", ok=True, checked_at=now - timedelta(days=3)))

    config = write_config(tmp_path, db)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "targets: [", "targets: [{name: idle, type: dns, host: b.test}, "
        ),
        encoding="utf-8",
    )
    with Storage(db) as storage:  # `idle` last ran 10 days ago: outside both windows
        storage.save(CheckResult("idle", ok=True, checked_at=now - timedelta(days=10)))

    result = runner.invoke(cli.app, ["status", "--config", str(config)])

    assert result.exit_code == 0
    assert "UPTIME 24H" in result.output
    assert "UPTIME 7D" in result.output
    web_line = next(line for line in result.output.splitlines() if line.startswith("web"))
    assert "75.00%" in web_line  # 24h: 3 of 4
    assert "80.00%" in web_line  # 7d: 4 of 5
    idle_line = next(line for line in result.output.splitlines() if line.startswith("idle"))
    assert "%" not in idle_line  # no checks in either window: dashes, not a crash or 0%
