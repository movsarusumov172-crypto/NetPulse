"""YAML configuration loading, with errors written for the person editing the file."""

import difflib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import ErrorDetails

from netpulse.models import DnsTarget, HttpTarget, Target, TargetField, TcpTarget


class ConfigError(Exception):
    """The configuration file is missing, malformed or invalid. The message is user-facing."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    interval: float = Field(default=30.0, gt=0, description="Seconds between checks of a target.")
    timeout: float = Field(default=10.0, gt=0, description="Seconds before a check is aborted.")
    concurrency: int = Field(default=10, ge=1, description="Max checks running at once.")
    retries: int = Field(default=3, ge=0, description="Extra attempts before a target is DOWN.")
    retry_base_delay: float = Field(default=1.0, ge=0, description="First retry delay, doubles.")
    ssl_expiry_warn_days: int = Field(default=14, ge=0)
    degraded_ms: float | None = Field(
        default=None, gt=0, description="Latency above this is DEGRADED. Unset = never degraded."
    )
    database: Path = Path("netpulse.db")
    metrics_host: str = "127.0.0.1"
    metrics_port: int = Field(default=9100, ge=0, le=65535)
    targets: list[TargetField] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_are_unique(self) -> "Settings":
        names = [target.name for target in self.targets]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"имена целей должны быть уникальными, повторяются: {', '.join(duplicates)}"
            )
        return self

    def interval_for(self, target: Target) -> float:
        return target.interval if target.interval is not None else self.interval

    def retries_for(self, target: Target) -> int:
        return target.retries if target.retries is not None else self.retries

    def degraded_ms_for(self, target: Target) -> float | None:
        return target.degraded_ms if target.degraded_ms is not None else self.degraded_ms


_TARGET_MODELS: dict[str, type[BaseModel]] = {
    "http": HttpTarget,
    "tcp": TcpTarget,
    "dns": DnsTarget,
}
_MAX_REPORTED_ERRORS = 15


def load_settings(path: Path) -> Settings:
    """Read and validate the config file. Raises `ConfigError` with a readable explanation."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"файл конфигурации {path} не найден") from exc
    except OSError as exc:
        raise ConfigError(f"не удалось прочитать {path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} должен быть в кодировке UTF-8") from exc

    try:
        raw = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"не удалось разобрать YAML в {path}{_yaml_position(exc)}") from exc
    if raw is None:
        raise ConfigError(f"{path} пуст: добавьте хотя бы список targets")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: на верхнем уровне нужен набор параметров (interval, targets, ...)"
        )

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        described = sorted(
            (_describe_error(error, raw, root) for error in exc.errors()), key=lambda item: item[0]
        )
        lines = [text for _, text in described]
        if len(lines) > _MAX_REPORTED_ERRORS:
            hidden = len(lines) - _MAX_REPORTED_ERRORS
            lines = [*lines[:_MAX_REPORTED_ERRORS], f"  ... и ещё ошибок: {hidden}"]
        raise ConfigError(f"ошибки в {path}:\n" + "\n".join(lines)) from exc


def _yaml_position(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    problem = getattr(exc, "problem", None)
    detail = f": {problem}" if problem else ""
    return f", строка {mark.line + 1}, столбец {mark.column + 1}{detail}"


def _describe_error(
    error: ErrorDetails, raw: dict[str, Any], root: yaml.Node | None
) -> tuple[int, str]:
    """One bullet line for the user, plus the YAML line it refers to (for ordering)."""
    loc = list(error["loc"])
    target_raw: Any = None
    if len(loc) >= 2 and loc[0] == "targets" and isinstance(loc[1], int):
        target_raw = _lookup(raw, loc[:2])
        name = target_raw.get("name") if isinstance(target_raw, dict) else None
        where = f"цель №{loc[1] + 1}" + (f" «{name}»" if name else "")
        # pydantic inserts the matched union member (http/tcp/dns) into the path; hide it.
        field_loc = loc[3:] if len(loc) > 2 and loc[2] in _TARGET_MODELS else loc[2:]
        loc = loc[:2] + field_loc
        field_path = ".".join(str(part) for part in field_loc)
        if field_path:
            where += f", параметр «{field_path}»"
    else:
        field_path = ".".join(str(part) for part in loc)
        where = f"параметр «{field_path}»" if field_path else "конфиг"

    line = _line_of(root, loc)
    position = f" (строка {line})" if line else ""
    return line or 0, f"  • {where}{position}: {_explain(error, target_raw, loc)}"


def _lookup(data: Any, loc: list[Any]) -> Any:
    for part in loc:
        try:
            data = data[part]
        except (KeyError, IndexError, TypeError):
            return None
    return data


def _line_of(root: yaml.Node | None, loc: list[Any]) -> int | None:
    """Line (1-based) of the deepest part of `loc` that exists in the YAML document."""
    node = root
    for part in loc:
        child = _child(node, part)
        if child is None:
            break
        node = child
    return node.start_mark.line + 1 if node is not None else None


def _child(node: yaml.Node | None, part: Any) -> yaml.Node | None:
    children: list[yaml.Node] = []
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value == str(part):
                children.append(value)
    elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value):
        children.append(node.value[part])
    return children[0] if children else None


def _explain(error: ErrorDetails, target_raw: Any, loc: list[Any]) -> str:
    kind = error["type"]
    ctx = error.get("ctx", {})
    match kind:
        case "missing":
            return "обязательный параметр не задан"
        case "extra_forbidden":
            return "неизвестный параметр" + _suggestion(loc, target_raw)
        case "union_tag_not_found":
            return "не указан type: допустимо http, tcp или dns"
        case "union_tag_invalid":
            given = target_raw.get("type") if isinstance(target_raw, dict) else None
            return f"неизвестный тип «{given}»: допустимо http, tcp или dns"
        case "greater_than":
            return f"значение должно быть больше {ctx['gt']:g}"
        case "greater_than_equal":
            return f"значение не должно быть меньше {ctx['ge']:g}"
        case "less_than_equal":
            return f"значение не должно быть больше {ctx['le']:g}"
        case "string_too_short":
            return "значение не может быть пустым"
        case "too_short":
            return "нужен хотя бы один элемент"
        case "int_parsing" | "int_type" | "int_from_float":
            return "нужно целое число"
        case "float_parsing" | "float_type":
            return "нужно число"
        case "string_type":
            return "нужна строка (числа и true/false возьмите в кавычки)"
        case "bool_parsing" | "bool_type":
            return "нужно true или false"
        case "url_parsing" | "url_scheme" | "url_type":
            return "некорректный URL: нужен адрес вида http://... или https://..."
        case "literal_error":
            expected = str(ctx.get("expected", "")).replace("'", "").replace(" or ", " или ")
            return f"допустимые значения: {expected}"
        case "dict_type" | "model_type" | "model_attributes_type":
            return "нужен набор параметров «ключ: значение»"
        case "list_type":
            return "нужен список"
        case "value_error":
            return str(ctx.get("error", error["msg"]))
        case _:
            return error["msg"]


def _suggestion(loc: list[Any], target_raw: Any) -> str:
    unknown = str(loc[-1]) if loc else ""
    if isinstance(target_raw, dict):
        model = _TARGET_MODELS.get(str(target_raw.get("type")))
        known = list(model.model_fields) if model else []
    else:
        known = list(Settings.model_fields)
    close = difflib.get_close_matches(unknown, known, n=1)
    return f" (возможно, «{close[0]}»?)" if close else ""
