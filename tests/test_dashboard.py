import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import generate_latest

from netpulse.dashboard import HISTORY_SIZE, StatusBoard, StatusServer
from netpulse.metrics import Metrics
from netpulse.models import CheckResult, DnsTarget, Target

WEB = DnsTarget(name="web", host="web.test")
DB = DnsTarget(name="db", host="db.test")
T0 = datetime(2030, 1, 1, tzinfo=UTC)


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
