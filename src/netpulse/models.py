"""Доменные модели: проверяемые цели и результаты проверок."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class _TargetBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)


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
"""Для кода; конфиг валидируется через `TargetField`, который выбирает модель по `type`."""

TargetField = Annotated[Target, Field(discriminator="type")]


def target_from_string(value: str) -> Target:
    """Собрать цель на лету: URL -> HTTP, `host:port` -> TCP, всё остальное -> DNS."""
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
