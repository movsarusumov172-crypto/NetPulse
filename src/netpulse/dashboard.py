"""Status page: latest results in memory and the HTTP server for page, JSON and metrics."""

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from netpulse.models import CheckResult, Incident, Target, Uptime
from netpulse.storage import Storage

logger = logging.getLogger(__name__)

HISTORY_SIZE = 30
INCIDENTS_DEFAULT_LIMIT = 20
INCIDENTS_MAX_LIMIT = 100
UPTIME_CACHE_SECONDS = 30.0


@dataclass
class _TargetStatus:
    target: Target
    last: CheckResult | None = None
    since: datetime | None = None  # when the target entered its current state
    checks: int = 0
    successes: int = 0
    history: deque[CheckResult] = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))


class StatusBoard:
    """Latest results per target. Written by the monitor loop, read by HTTP threads."""

    def __init__(
        self, targets: list[Target], interval: float, *, degraded_ms: float | None = None
    ) -> None:
        self._interval = interval
        self._degraded_ms = degraded_ms
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

    def target_names(self) -> list[str]:
        with self._lock:
            return list(self._statuses)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            targets = [
                _describe(status, self._interval, self._degraded_ms)
                for status in self._statuses.values()
            ]
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "interval": self._interval,
            "targets": targets,
        }


def _state(last: CheckResult | None) -> str:
    if last is None:
        return "pending"
    if not last.ok:
        return "down"
    return "degraded" if last.degraded else "up"


def _describe(
    status: _TargetStatus, default_interval: float, default_degraded_ms: float | None
) -> dict[str, Any]:
    last = status.last
    state = _state(last)
    degraded_ms = status.target.degraded_ms
    return {
        "name": status.target.name,
        "type": status.target.type,
        "address": status.target.address,
        "state": state,
        "interval": status.target.interval
        if status.target.interval is not None
        else default_interval,
        "degraded_ms": degraded_ms if degraded_ms is not None else default_degraded_ms,
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


class _UptimeCache:
    """Uptime scans a week of results, so every page refresh must not repeat it."""

    def __init__(self, db_path: Path | None, ttl: float) -> None:
        self._db_path = db_path
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: dict[str, Uptime] = {}
        self._expires = 0.0

    def get(self) -> dict[str, Uptime]:
        if self._db_path is None:
            return {}
        with self._lock:
            if time.monotonic() >= self._expires:
                with Storage(self._db_path, readonly=True) as storage:
                    self._value = storage.uptime(datetime.now(UTC))
                self._expires = time.monotonic() + self._ttl
            return self._value


class _BadRequest(Exception):
    """A malformed query parameter; the message is returned to the client."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        registry: CollectorRegistry,
        board: StatusBoard,
        db_path: Path | None,
        uptime_ttl: float,
    ) -> None:
        super().__init__(address, _Handler)
        self.registry = registry
        self.board = board
        self.db_path = db_path
        self.uptime = _UptimeCache(db_path, uptime_ttl)
        self.page = files("netpulse").joinpath("static/dashboard.html").read_bytes()


def _incident_json(incident: Incident, now: datetime) -> dict[str, Any]:
    return {
        "id": incident.id,
        "target": incident.target,
        "started_at": incident.started_at.isoformat(),
        "ended_at": incident.ended_at.isoformat() if incident.ended_at else None,
        "ongoing": incident.ended_at is None,
        "duration_seconds": int(incident.duration(now).total_seconds()),
        "first_error": incident.first_error,
    }


def _int_param(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    raw = query.get(name, [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise _BadRequest(f"{name} must be an integer from {low} to {high}")
    return value


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        try:
            self._route(url.path, parse_qs(url.query))
        except _BadRequest as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            logger.exception("Request to %s failed", url.path)
            self._send_json({"error": "internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _route(self, path: str, query: dict[str, list[str]]) -> None:
        server = cast(_Server, self.server)
        if path == "/metrics":
            self._send(generate_latest(server.registry), CONTENT_TYPE_LATEST)
        elif path == "/api/status":
            self._send_json(self._status(server))
        elif path == "/api/incidents":
            self._incidents(server, query)
        elif path == "/":
            self._send(server.page, "text/html; charset=utf-8")
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    @staticmethod
    def _status(server: _Server) -> dict[str, Any]:
        snapshot = server.board.snapshot()
        uptimes = server.uptime.get()
        for target in snapshot["targets"]:
            uptime = uptimes.get(target["name"])
            target["uptime_24h"] = uptime.day if uptime else None
            target["uptime_7d"] = uptime.week if uptime else None
        return snapshot

    def _incidents(self, server: _Server, query: dict[str, list[str]]) -> None:
        limit = _int_param(query, "limit", INCIDENTS_DEFAULT_LIMIT, 1, INCIDENTS_MAX_LIMIT)
        names = server.board.target_names()
        target = query.get("target", [None])[0]
        if target is not None:
            if target not in names:
                self._send_json({"error": "unknown target"}, HTTPStatus.NOT_FOUND)
                return
            names = [target]
        incidents: list[Incident] = []
        if server.db_path is not None:
            with Storage(server.db_path, readonly=True) as storage:
                incidents = storage.incidents(limit=limit, targets=names)
        now = datetime.now(UTC)
        self._send_json({"incidents": [_incident_json(item, now) for item in incidents]})

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self._send(body, "application/json; charset=utf-8", status)

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the page polls every 5 seconds; no need to log each request


class StatusServer:
    """Serves `/` (status page), `/api/*` (JSON) and `/metrics` (Prometheus).

    `db_path` is where history endpoints (incidents, uptime) read from, each request through
    its own read-only connection; without it they return empty data. Uptime is cached for
    `uptime_ttl` seconds.
    """

    def __init__(
        self,
        registry: CollectorRegistry,
        board: StatusBoard,
        db_path: Path | None = None,
        *,
        uptime_ttl: float = UPTIME_CACHE_SECONDS,
    ) -> None:
        self._registry = registry
        self._board = board
        self._db_path = db_path
        self._uptime_ttl = uptime_ttl
        self._server: _Server | None = None

    def start(self, host: str, port: int) -> int:
        """Start the server in a background thread. Returns the bound port."""
        self._server = _Server(
            (host, port), self._registry, self._board, self._db_path, self._uptime_ttl
        )
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        ).start()
        return int(self._server.server_port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
