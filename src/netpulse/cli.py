"""Интерфейс командной строки."""

import asyncio
import contextlib
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from netpulse.checks import run_check
from netpulse.config import ConfigError, Settings, load_settings
from netpulse.models import CheckResult, target_from_string
from netpulse.monitor import run_monitor
from netpulse.storage import Storage

app = typer.Typer(
    help="Асинхронный мониторинг доступности HTTP-, TCP- и DNS-целей.", no_args_is_help=True
)

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", envvar="NETPULSE_CONFIG", help="Путь к YAML-конфигу."),
]


def _load(path: Path) -> Settings:
    try:
        return load_settings(path)
    except ConfigError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _format_result(result: CheckResult) -> str:
    parts = ["OK  " if result.ok else "FAIL", result.target]
    if result.status_code is not None:
        parts.append(f"status={result.status_code}")
    if result.latency_ms is not None:
        parts.append(f"{result.latency_ms:.0f}ms")
    if result.ssl_days_left is not None:
        parts.append(f"ssl={result.ssl_days_left}d")
    if result.error:
        parts.append(f"- {result.error}")
    return " ".join(parts)


@app.command()
def run(config: ConfigOption = Path("config.yaml")) -> None:
    """Мониторить все цели из конфига, пока процесс не остановят."""
    settings = _load(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx пишет каждый запрос на уровне INFO, а URL запроса к Telegram содержит токен бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Как PID 1 в контейнере процесс иначе игнорировал бы SIGTERM.
    # Выход даёт asyncio завершиться штатно.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_monitor(settings))


@app.command()
def check(
    target: Annotated[str, typer.Argument(help="URL http(s)://, host:port или DNS-имя.")],
    timeout: Annotated[float, typer.Option(help="Секунд до прерывания проверки.")] = 10.0,
    ssl_warn_days: Annotated[
        int, typer.Option(help="Считать цель упавшей, если сертификат истекает раньше.")
    ] = 14,
) -> None:
    """Проверить одну цель прямо сейчас. Код выхода 1, если цель недоступна."""
    result = asyncio.run(run_check(target_from_string(target), timeout, ssl_warn_days))
    typer.echo(_format_result(result))
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def status(config: ConfigOption = Path("config.yaml")) -> None:
    """Показать последний результат и аптайм за 24 часа для каждой цели из конфига."""
    settings = _load(config)
    if not settings.database.exists():
        typer.echo(f"No database at {settings.database}: has `netpulse run` started yet?", err=True)
        raise typer.Exit(1)

    with Storage(settings.database) as storage:
        results = storage.latest()
        uptime = storage.uptime_ratios(datetime.now(UTC) - timedelta(hours=24))

    configured = {target.name for target in settings.targets}
    rows = [
        (
            result.target,
            "UP" if result.ok else "DOWN",
            f"{result.latency_ms:.0f} ms" if result.latency_ms is not None else "-",
            f"{uptime[result.target]:.1%}",
            result.checked_at.strftime("%Y-%m-%d %H:%M:%S"),
            result.error or "",
        )
        for result in results
        if result.target in configured
    ]
    if not rows:
        typer.echo("No results yet.")
        return
    header = ("TARGET", "STATE", "LATENCY", "UPTIME 24H", "CHECKED (UTC)", "ERROR")
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        typer.echo(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


if __name__ == "__main__":
    app()
