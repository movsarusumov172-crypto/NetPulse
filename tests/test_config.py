from pathlib import Path

import pytest

from netpulse.config import ConfigError, load_settings
from netpulse.models import DnsTarget, HttpTarget, TcpTarget


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_all_target_types_and_defaults(tmp_path: Path) -> None:
    settings = load_settings(
        write(
            tmp_path,
            """
interval: 15
targets:
  - {name: web, type: http, url: "https://example.com", expected_status: 204}
  - {name: db, type: tcp, host: db.internal, port: 5432}
  - {name: dns, type: dns, host: example.com}
""",
        )
    )
    web, db, dns = settings.targets
    assert settings.interval == 15
    assert settings.retries == 3  # default
    assert isinstance(web, HttpTarget)
    assert web.expected_status == 204
    assert isinstance(db, TcpTarget)
    assert isinstance(dns, DnsTarget)


def test_bundled_config_is_valid() -> None:
    assert load_settings(Path(__file__).parent.parent / "config.yaml").targets


@pytest.mark.parametrize(
    "body",
    [
        "targets: []",
        "targets: [{name: db, type: tcp, host: db.internal}]",  # missing port
        "targets: [{name: x, type: ftp, host: h}]",  # unknown type
        "targets: [{name: x, type: http, url: not-a-url}]",
        "interval: 0\ntargets: [{name: x, type: dns, host: h}]",
        "intervall: 5\ntargets: [{name: x, type: dns, host: h}]",  # misspelled key
        "targets: [{name: x, type: dns, host: a}, {name: x, type: dns, host: b}]",  # duplicate
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, body))


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_degraded_threshold_can_be_global_and_overridden_per_target(tmp_path: Path) -> None:
    settings = load_settings(
        write(
            tmp_path,
            """
degraded_ms: 500
targets:
  - {name: fast, type: dns, host: a.test}
  - {name: slow-by-nature, type: dns, host: b.test, degraded_ms: 3000}
""",
        )
    )
    fast, slow = settings.targets

    assert settings.degraded_ms_for(fast) == 500
    assert settings.degraded_ms_for(slow) == 3000


def test_degradation_is_off_unless_a_threshold_is_configured(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, "targets: [{name: x, type: dns, host: h}]"))

    assert settings.degraded_ms_for(settings.targets[0]) is None


@pytest.mark.parametrize("value", ["0", "-5"])
def test_non_positive_degraded_threshold_is_rejected(tmp_path: Path, value: str) -> None:
    body = f"degraded_ms: {value}\ntargets: [{{name: x, type: dns, host: h}}]"
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, body))
