import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import generate_latest

from netpulse.dashboard import HISTORY_SIZE, StatusBoard, StatusServer
from netpulse.metrics import Metrics
from netpulse.models import CheckResult, DnsTarget, Target
from netpulse.storage import Storage

WEB = DnsTarget(name="web", host="web.test")
DB = DnsTarget(name="db", host="db.test")
T0 = datetime(2030, 1, 1, tzinfo=UTC)
START = datetime.now(UTC) - timedelta(days=1)


def at(minutes: int, *, ok: bool, target: str = "web") -> CheckResult:
    return CheckResult(
        target, ok=ok, latency_ms=20.0 if ok else None, checked_at=T0 + timedelta(minutes=minutes)
    )


def by_name(board: StatusBoard) -> dict[str, dict[str, object]]:
    return {target["name"]: target for target in board.snapshot()["targets"]}


def test_targets_without_results_are_pending() -> None:
    board = StatusBoard([WEB, DB], interval=30)

    targets = by_name(board)

    assert targets["web"]["state"] == "pending"
    assert targets["web"]["uptime"] is None
    assert board.snapshot()["interval"] == 30


def test_since_moves_only_when_the_state_changes() -> None:
    board = StatusBoard([WEB], interval=30)
    for minute, ok in [(0, True), (1, True), (2, False), (3, False)]:
        board.record(at(minute, ok=ok))

    web = by_name(board)["web"]

    assert web["state"] == "down"
    assert web["since"] == (T0 + timedelta(minutes=2)).isoformat()
    assert web["uptime"] == 0.5
    assert web["checks"] == 4


def test_history_keeps_only_the_latest_checks() -> None:
    board = StatusBoard([WEB], interval=30)
    for minute in range(HISTORY_SIZE + 5):
        board.record(at(minute, ok=minute % 2 == 0))

    history = by_name(board)["web"]["history"]

    assert isinstance(history, list)
    assert len(history) == HISTORY_SIZE


def test_results_for_unknown_targets_are_ignored() -> None:
    board = StatusBoard([WEB], interval=30)
    board.record(at(0, ok=True, target="removed-from-config"))

    assert list(by_name(board)) == ["web"]


@pytest.fixture
def server() -> Iterator[tuple[str, Metrics]]:
    targets: list[Target] = [WEB]
    metrics = Metrics()
    board = StatusBoard(targets, interval=30)
    result = at(0, ok=True)
    metrics.record(result)
    board.record(result)
    status_server = StatusServer(metrics.registry, board)
    port = status_server.start("127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{port}", metrics
    finally:
        status_server.stop()


def get(url: str) -> tuple[str, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.headers["Content-Type"], response.read().decode()


def test_server_keeps_serving_prometheus_metrics(server: tuple[str, Metrics]) -> None:
    base, metrics = server

    _, body = get(f"{base}/metrics")

    assert 'netpulse_up{target="web"} 1.0' in body
    assert body == generate_latest(metrics.registry).decode()


def test_server_serves_status_json_and_page(server: tuple[str, Metrics]) -> None:
    base, _ = server

    json_type, status = get(f"{base}/api/status")
    page_type, page = get(f"{base}/")

    assert json_type.startswith("application/json")
    assert json.loads(status)["targets"][0]["state"] == "up"
    assert page_type.startswith("text/html")
    assert "NetPulse" in page


def test_unknown_paths_are_404(server: tuple[str, Metrics]) -> None:
    base, _ = server

    with pytest.raises(urllib.error.HTTPError) as error:
        get(f"{base}/nope")

    assert error.value.code == 404


def get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())  # type: ignore[no-any-return]


def error_of(url: str) -> tuple[int, str]:
    with pytest.raises(urllib.error.HTTPError) as error:
        get(url)
    return error.value.code, json.loads(error.value.read())["error"]


@pytest.fixture
def incident_server(tmp_path: Path) -> Iterator[str]:
    board = StatusBoard([WEB, DB], interval=30)
    with Storage(tmp_path / "np.db") as storage:  # stays open: the server reads next to it
        for minute, name in enumerate(["web", "db", "web", "removed-from-config"]):
            storage.open_incident(name, START + timedelta(minutes=minute), f"{name} failed")
            if name != "db":
                storage.close_incident(name, START + timedelta(minutes=minute, seconds=45))
        server = StatusServer(Metrics().registry, board, tmp_path / "np.db")
        port = server.start("127.0.0.1", 0)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.stop()


def test_incidents_endpoint_lists_newest_first_for_configured_targets_only(
    incident_server: str,
) -> None:
    incidents = get_json(f"{incident_server}/api/incidents")["incidents"]

    assert [i["target"] for i in incidents] == ["web", "db", "web"]
    newest = incidents[0]
    assert newest["first_error"] == "web failed"
    assert newest["ongoing"] is False
    assert newest["duration_seconds"] == 45
    assert newest["started_at"] == (START + timedelta(minutes=2)).isoformat()


def test_open_incident_is_ongoing_with_a_growing_duration(incident_server: str) -> None:
    incidents = get_json(f"{incident_server}/api/incidents?target=db")["incidents"]

    [db_incident] = incidents
    assert db_incident["ongoing"] is True
    assert db_incident["ended_at"] is None
    assert db_incident["duration_seconds"] >= 23 * 3600  # started a day ago


def test_incidents_can_be_filtered_by_target_and_limited(incident_server: str) -> None:
    web = get_json(f"{incident_server}/api/incidents?target=web")["incidents"]
    limited = get_json(f"{incident_server}/api/incidents?limit=1")["incidents"]

    assert [i["target"] for i in web] == ["web", "web"]
    assert len(limited) == 1


def test_incidents_endpoint_validates_its_parameters(incident_server: str) -> None:
    assert error_of(f"{incident_server}/api/incidents?target=nope") == (404, "unknown target")
    for bad_limit in ("0", "101", "abc", "-1"):
        code, message = error_of(f"{incident_server}/api/incidents?limit={bad_limit}")
        assert code == 400
        assert "limit" in message


def test_incidents_endpoint_without_a_database_is_empty(server: tuple[str, Metrics]) -> None:
    base, _ = server

    assert get_json(f"{base}/api/incidents") == {"incidents": []}


def _server_with_history(tmp_path: Path, **kwargs: float) -> tuple[StatusServer, Storage, str]:
    storage = Storage(tmp_path / "np.db")
    board = StatusBoard([WEB], interval=30)
    server = StatusServer(Metrics().registry, board, tmp_path / "np.db", **kwargs)
    return server, storage, f"http://127.0.0.1:{server.start('127.0.0.1', 0)}"


def test_status_takes_uptime_from_the_database_not_from_this_process(tmp_path: Path) -> None:
    server, storage, base = _server_with_history(tmp_path, uptime_ttl=0)
    now = datetime.now(UTC)
    try:
        # History written by an earlier run of the monitor: the board knows nothing about it.
        for hours_ago, ok in [(30, False), (3, True), (2, True), (1, False), (0.5, True)]:
            storage.save(CheckResult("web", ok=ok, checked_at=now - timedelta(hours=hours_ago)))

        [web] = get_json(f"{base}/api/status")["targets"]

        assert web["state"] == "pending"
        assert web["checks"] == 0
        assert web["uptime_24h"] == 0.75
        assert web["uptime_7d"] == 0.6
    finally:
        server.stop()
        storage.close()


def test_uptime_is_cached_between_requests(tmp_path: Path) -> None:
    server, storage, base = _server_with_history(tmp_path, uptime_ttl=3600)
    try:
        storage.save(CheckResult("web", ok=True))
        assert get_json(f"{base}/api/status")["targets"][0]["uptime_24h"] == 1.0

        storage.save(CheckResult("web", ok=False))
        assert get_json(f"{base}/api/status")["targets"][0]["uptime_24h"] == 1.0  # still cached
    finally:
        server.stop()
        storage.close()


def test_status_without_a_database_has_no_uptime(server: tuple[str, Metrics]) -> None:
    base, _ = server

    [web] = get_json(f"{base}/api/status")["targets"]

    assert web["uptime_24h"] is None
    assert web["uptime_7d"] is None


def test_a_slow_answer_is_degraded_not_down_and_uses_the_effective_threshold() -> None:
    plain = DnsTarget(name="plain", host="p.test")
    custom = DnsTarget(name="custom", host="c.test", degraded_ms=2000)
    board = StatusBoard([plain, custom], interval=30, degraded_ms=500)
    board.record(CheckResult("plain", ok=True, latency_ms=900.0, degraded=True, checked_at=T0))
    board.record(CheckResult("custom", ok=False, error="boom", checked_at=T0))

    targets = by_name(board)

    assert targets["plain"]["state"] == "degraded"
    assert targets["plain"]["degraded_ms"] == 500
    assert targets["custom"]["state"] == "down"
    assert targets["custom"]["degraded_ms"] == 2000
