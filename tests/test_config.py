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


def test_targets_can_override_interval_timeout_and_retries(tmp_path: Path) -> None:
    settings = load_settings(
        write(
            tmp_path,
            """
interval: 30
retries: 3
targets:
  - {name: default, type: dns, host: a.test}
  - {name: custom, type: dns, host: b.test, interval: 5, retries: 0, timeout: 2}
""",
        )
    )
    default, custom = settings.targets

    assert (settings.interval_for(default), settings.retries_for(default)) == (30, 3)
    assert (settings.interval_for(custom), settings.retries_for(custom)) == (5, 0)
    assert custom.timeout == 2
    assert default.timeout is None


def test_http_target_accepts_method_headers_and_expected_text(tmp_path: Path) -> None:
    settings = load_settings(
        write(
            tmp_path,
            """
targets:
  - name: api
    type: http
    url: https://api.example.com/health
    method: post
    headers: {X-Env: prod}
    body_contains: '"status":"ok"'
""",
        )
    )
    [api] = settings.targets

    assert isinstance(api, HttpTarget)
    assert api.method == "POST"
    assert api.headers == {"X-Env": "prod"}
    assert api.body_contains == '"status":"ok"'


def test_header_values_can_reference_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_TOKEN", "s3cret")
    body = """
targets:
  - name: api
    type: http
    url: https://api.example.com
    headers: {Authorization: "Bearer ${API_TOKEN}"}
"""

    [api] = load_settings(write(tmp_path, body)).targets

    assert isinstance(api, HttpTarget)
    assert api.headers["Authorization"] == "Bearer s3cret"


def test_unset_environment_variable_in_a_header_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_TOKEN", raising=False)
    body = """
targets:
  - name: api
    type: http
    url: https://api.example.com
    headers: {Authorization: "Bearer ${API_TOKEN}"}
"""

    with pytest.raises(ConfigError, match=r"API_TOKEN.*не задана"):
        load_settings(write(tmp_path, body))


def error_text(tmp_path: Path, body: str) -> str:
    with pytest.raises(ConfigError) as error:
        load_settings(write(tmp_path, body))
    return str(error.value)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # a misspelled key: points at the line and suggests the right name
        ("intervall: 5\ntargets: [{name: x, type: dns, host: h}]", ["строка 1", "«interval»"]),
        # a target without its required field names the target and the field
        (
            "targets:\n  - {name: db, type: tcp, host: db.internal}",
            ["цель №1 «db»", "параметр «port»", "строка 2", "не задан"],
        ),
        # unknown target type lists the valid ones
        (
            "targets:\n  - {name: x, type: ftp, host: h}",
            ["цель №1 «x»", "неизвестный тип «ftp»", "http, tcp или dns"],
        ),
        ("targets:\n  - {name: x, host: h}", ["не указан type"]),
        # wrong value types and ranges
        ("interval: 0\ntargets: [{name: x, type: dns, host: h}]", ["«interval»", "больше 0"]),
        ("retries: many\ntargets: [{name: x, type: dns, host: h}]", ["«retries»", "целое число"]),
        (
            "targets: [{name: x, type: http, url: example.com}]",
            ["параметр «url»", "http://... или https://..."],
        ),
        (
            "targets: [{name: x, type: http, url: 'http://a.test', method: FETCH}]",
            ["параметр «method»", "GET, HEAD, POST, PUT, PATCH, DELETE или OPTIONS"],
        ),
        (
            "targets: [{name: x, type: http, url: 'http://a.test', headers: {X-Retry: 3}}]",
            ["headers.X-Retry", "в кавычки"],
        ),
        (
            "targets: [{name: x, type: http, url: http://a.test, method: HEAD, body_contains: o}]",
            ["HEAD"],
        ),
        # whole-file problems
        ("targets: []", ["параметр «targets»", "хотя бы один"]),
        (
            "targets: [{name: x, type: dns, host: a}, {name: x, type: dns, host: b}]",
            ["уникальными", "x"],
        ),
    ],
)
def test_config_errors_say_what_where_and_how_to_fix(
    tmp_path: Path, body: str, expected: list[str]
) -> None:
    message = error_text(tmp_path, body)

    for fragment in expected:
        assert fragment in message
    # no leaked library internals
    assert "validation error" not in message
    assert "pydantic" not in message.lower()
    assert "type=" not in message


def test_every_problem_is_reported_at_once_in_file_order(tmp_path: Path) -> None:
    body = """interval: 0
targets:
  - {name: a, type: tcp, host: h}
  - {name: b, type: dns, host: h, timeout: -1}
"""

    lines = error_text(tmp_path, body).splitlines()[1:]

    assert len(lines) == 3
    assert "строка 1" in lines[0]
    assert "строка 3" in lines[1]
    assert "строка 4" in lines[2]


def test_yaml_syntax_error_points_at_the_line(tmp_path: Path) -> None:
    message = error_text(tmp_path, "interval: 30\ntargets: [unclosed\n")

    assert "YAML" in message
    assert "строка" in message


@pytest.mark.parametrize(
    ("body", "fragment"),
    [("", "пуст"), ("   \n# only a comment\n", "пуст"), ("- just\n- a list\n", "верхнем уровне")],
)
def test_empty_or_wrongly_shaped_files_are_explained(
    tmp_path: Path, body: str, fragment: str
) -> None:
    assert fragment in error_text(tmp_path, body)


def test_missing_config_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="не найден") as error:
        load_settings(tmp_path / "nope.yaml")

    assert "nope.yaml" in str(error.value)
