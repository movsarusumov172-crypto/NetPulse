"""YAML configuration loading."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from netpulse.models import TargetField


class ConfigError(Exception):
    """The configuration file is missing, malformed or invalid."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    interval: float = Field(default=30.0, gt=0, description="Seconds between checks of a target.")
    timeout: float = Field(default=10.0, gt=0, description="Seconds before a check is aborted.")
    concurrency: int = Field(default=10, ge=1, description="Max checks running at once.")
    retries: int = Field(default=3, ge=0, description="Extra attempts before a target is DOWN.")
    retry_base_delay: float = Field(default=1.0, ge=0, description="First retry delay, doubles.")
    ssl_expiry_warn_days: int = Field(default=14, ge=0)
    database: Path = Path("netpulse.db")
    metrics_host: str = "127.0.0.1"
    metrics_port: int = Field(default=9100, ge=0, le=65535)
    targets: list[TargetField] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_are_unique(self) -> "Settings":
        names = [target.name for target in self.targets]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate target names: {', '.join(duplicates)}")
        return self


def load_settings(path: Path) -> Settings:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc
