"""Domain models: monitored targets and check results."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class _TargetBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    degraded_ms: float | None = Field(
        default=None, gt=0, description="Latency above this is DEGRADED; overrides the global one."
    )


class HttpTarget(_TargetBase):
    type: Literal["http"] = "http"
    url: HttpUrl
    expected_status: int = Field(default=200, ge=100, le=599)

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
