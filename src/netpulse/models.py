"""Domain models: monitored targets and check results."""

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _expand_env(value: str) -> str:
    """Replace `${NAME}` with the environment variable, so secrets stay out of config files."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"переменная окружения {name} не задана")
        return os.environ[name]

    return _ENV_REFERENCE.sub(replace, value)


class _TargetBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    # Per-target overrides of the global settings; None = use the global value.
    interval: float | None = Field(default=None, gt=0)
    timeout: float | None = Field(default=None, gt=0)
    retries: int | None = Field(default=None, ge=0)
    degraded_ms: float | None = Field(default=None, gt=0)
    paused: bool = False  # maintenance: no checks, no alerts


class HttpTarget(_TargetBase):
    type: Literal["http"] = "http"
    url: HttpUrl
    expected_status: int = Field(default=200, ge=100, le=599)
    method: HttpMethod = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body_contains: str | None = Field(default=None, min_length=1)

    @field_validator("method", mode="before")
    @classmethod
    def _method_is_case_insensitive(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("headers")
    @classmethod
    def _expand_env_in_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        return {name: _expand_env(value) for name, value in headers.items()}

    @model_validator(mode="after")
    def _head_responses_have_no_body(self) -> "HttpTarget":
        if self.method == "HEAD" and self.body_contains is not None:
            raise ValueError("body_contains нельзя использовать с методом HEAD: у ответа нет тела")
        return self

    @property
    def address(self) -> str:
        return str(self.url)


class TcpTarget(_TargetBase):
    type: Literal["tcp"] = "tcp"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class DnsTarget(_TargetBase):
    type: Literal["dns"] = "dns"
    host: str = Field(min_length=1)

    @property
    def address(self) -> str:
        return self.host


Target = HttpTarget | TcpTarget | DnsTarget
"""Used in code; configs validate against `TargetField`, which picks the model by `type`."""

TargetField = Annotated[Target, Field(discriminator="type")]


def target_from_string(value: str) -> Target:
    """Build an ad-hoc target: URL -> HTTP, `host:port` -> TCP, anything else -> DNS."""
    if value.startswith(("http://", "https://")):
        return HttpTarget(name=value, url=HttpUrl(value))
    host, sep, port = value.rpartition(":")
    if sep and port.isdigit():
        return TcpTarget(name=value, host=host, port=int(port))
    return DnsTarget(name=value, host=value)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CheckResult:
    target: str
    ok: bool
    latency_ms: float | None = None
    status_code: int | None = None
    ssl_days_left: int | None = None
    error: str | None = None
    checked_at: datetime = field(default_factory=_utcnow)
    degraded: bool = False
    """The target answered (`ok`) but slower than its latency threshold."""


@dataclass(frozen=True, slots=True)
class Uptime:
    """Share of successful checks (0..1) over the last 24 hours and 7 days; None = no checks."""

    day: float | None
    week: float | None


@dataclass(frozen=True, slots=True)
class Incident:
    """A period during which a target was down. `ended_at` is None while it is ongoing."""

    id: int
    target: str
    started_at: datetime
    ended_at: datetime | None
    first_error: str | None

    def duration(self, now: datetime) -> timedelta:
        return max(timedelta(0), (self.ended_at or now) - self.started_at)
