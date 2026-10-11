import pytest

from netpulse.models import CheckResult


@pytest.fixture
def up() -> CheckResult:
    return CheckResult("svc", ok=True, latency_ms=12.0)


@pytest.fixture
def down() -> CheckResult:
    return CheckResult("svc", ok=False, error="boom")
