from netpulse.metrics import Metrics
from netpulse.models import CheckResult


def sample(metrics: Metrics, name: str, **labels: str) -> float | None:
    return metrics.registry.get_sample_value(name, labels)


def test_record_success_sets_up_latency_and_ssl() -> None:
    metrics = Metrics()
    metrics.record(CheckResult("web", ok=True, latency_ms=250.0, ssl_days_left=30))

    assert sample(metrics, "netpulse_up", target="web") == 1
    assert sample(metrics, "netpulse_latency_seconds", target="web") == 0.25
    assert sample(metrics, "netpulse_ssl_expiry_days", target="web") == 30
    assert sample(metrics, "netpulse_checks_total", target="web", result="ok") == 1


def test_failure_without_response_drops_stale_latency() -> None:
    metrics = Metrics()
    metrics.record(CheckResult("web", ok=True, latency_ms=250.0))
    metrics.record(CheckResult("web", ok=False, error="timeout"))

    assert sample(metrics, "netpulse_up", target="web") == 0
    assert sample(metrics, "netpulse_latency_seconds", target="web") is None
    assert sample(metrics, "netpulse_checks_total", target="web", result="fail") == 1
    assert sample(metrics, "netpulse_checks_total", target="web", result="ok") == 1


def test_failure_for_never_successful_target_does_not_raise() -> None:
    Metrics().record(CheckResult("web", ok=False, error="down"))
