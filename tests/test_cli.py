from datetime import UTC, datetime
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
