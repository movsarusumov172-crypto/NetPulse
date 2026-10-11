"""Prometheus metrics and the HTTP endpoint that exposes them."""

from contextlib import suppress
from http.server import HTTPServer

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    disable_created_metrics,
    start_http_server,
)

from netpulse.models import CheckResult

disable_created_metrics()  # type: ignore[no-untyped-call]  # drop the noisy *_created series


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self._up = Gauge(
            "netpulse_up",
            "1 if the last check of the target succeeded, 0 otherwise.",
            ["target"],
            registry=self.registry,
        )
        self._latency = Gauge(
            "netpulse_latency_seconds",
            "Response time of the last check; absent while the target is failing to answer.",
            ["target"],
            registry=self.registry,
        )
        self._ssl_days = Gauge(
            "netpulse_ssl_expiry_days",
            "Days until the TLS certificate of an HTTPS target expires.",
            ["target"],
            registry=self.registry,
        )
        self._checks = Counter(
            "netpulse_checks_total",
            "Completed checks (after retries) by outcome.",
            ["target", "result"],
            registry=self.registry,
        )
        self._server: HTTPServer | None = None

    def record(self, result: CheckResult) -> None:
        target = result.target
        self._up.labels(target).set(int(result.ok))
        self._checks.labels(target, "ok" if result.ok else "fail").inc()
        self._set_or_clear(self._latency, target, _seconds(result.latency_ms))
        self._set_or_clear(self._ssl_days, target, result.ssl_days_left)

    @staticmethod
    def _set_or_clear(gauge: Gauge, target: str, value: float | None) -> None:
        if value is not None:
            gauge.labels(target).set(value)
            return
        with suppress(KeyError):  # nothing to clear if the series was never set
            gauge.remove(target)

    def start(self, host: str, port: int) -> int:
        """Serve `/metrics` in a background thread. Returns the bound port."""
        self._server, _ = start_http_server(port, addr=host, registry=self.registry)
        return int(self._server.server_port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _seconds(milliseconds: float | None) -> float | None:
    return None if milliseconds is None else milliseconds / 1000
