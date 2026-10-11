"""Страница статуса: последние результаты в памяти и HTTP-сервер для них и для Prometheus."""

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, cast

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from netpulse.models import CheckResult, Target

HISTORY_SIZE = 30


@dataclass
class _TargetStatus:
    target: Target
    last: CheckResult | None = None
    since: datetime | None = None  # когда цель пришла в текущее состояние
    checks: int = 0
    successes: int = 0
    history: deque[CheckResult] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))


class StatusBoard:
    """Последние результаты по каждой цели. Пишет цикл мониторинга, читает HTTP-поток."""

    def __init__(self, targets: list[Target], interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._statuses = {target.name: _TargetStatus(target) for target in targets}

    def record(self, result: CheckResult) -> None:
        with self._lock:
            status = self._statuses.get(result.target)
            if status is None:
                return
            if status.last is None or status.last.ok != result.ok:
                status.since = result.checked_at
            status.last = result
            status.checks += 1
            status.successes += result.ok
            status.history.append(result)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            targets = [_describe(status) for status in self._statuses.values()]
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "interval": self._interval,
            "targets": targets,
        }


def _describe(status: _TargetStatus) -> dict[str, Any]:
    last = status.last
    state = "pending" if last is None else ("up" if last.ok else "down")
    return {
        "name": status.target.name,
        "type": status.target.type,
        "address": status.target.address,
        "state": state,
        "latency_ms": last.latency_ms if last else None,
        "status_code": last.status_code if last else None,
        "ssl_days_left": last.ssl_days_left if last else None,
        "error": last.error if last else None,
        "checked_at": last.checked_at.isoformat() if last else None,
        "since": status.since.isoformat() if status.since else None,
        "checks": status.checks,
        "uptime": status.successes / status.checks if status.checks else None,
        "history": [{"ok": item.ok, "latency_ms": item.latency_ms} for item in status.history],
    }


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], registry: CollectorRegistry, board: StatusBoard
    ) -> None:
        super().__init__(address, _Handler)
        self.registry = registry
        self.board = board
        self.page = files("netpulse").joinpath("static/dashboard.html").read_bytes()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = cast(_Server, self.server)
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            self._send(generate_latest(server.registry), CONTENT_TYPE_LATEST)
        elif path == "/api/status":
            body = json.dumps(server.board.snapshot(), ensure_ascii=False).encode()
            self._send(body, "application/json; charset=utf-8")
        elif path == "/":
            self._send(server.page, "text/html; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # каждый запрос страницы раз в 5 секунд в логе не нужен


class StatusServer:
    """Отдаёт `/` (страница статуса), `/api/status` (JSON) и `/metrics` (Prometheus)."""

    def __init__(self, registry: CollectorRegistry, board: StatusBoard) -> None:
        self._registry = registry
        self._board = board
        self._server: _Server | None = None

    def start(self, host: str, port: int) -> int:
        """Запускает сервер в фоновом потоке. Возвращает занятый порт."""
        self._server = _Server((host, port), self._registry, self._board)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return int(self._server.server_port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
