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
        "intervall: 5\ntargets: [{name: x, type: dns, host: h}]",  # typo'd key
        "targets: [{name: x, type: dns, host: a}, {name: x, type: dns, host: b}]",  # duplicate
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, body))


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")
