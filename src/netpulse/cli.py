"""Command-line interface."""

import asyncio
import contextlib
import difflib
import logging
import math
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from netpulse.checks import run_check
from netpulse.config import ConfigError, Settings, load_settings
from netpulse.models import CheckResult, Target, Uptime, target_from_string
from netpulse.monitor import run_monitor
from netpulse.storage import Storage

app = typer.Typer(help="Async uptime monitor for HTTP, TCP and DNS targets.", no_args_is_help=True)

ConfigOption = Annotated[
    Path,
    typer.Option("--config", "-c", envvar="NETPULSE_CONFIG", help="Path to the YAML config."),
]


def _load(path: Path) -> Settings:
    try:
        return load_settings(path)
    except ConfigError as exc:
        typer.echo(f"Ошибка: {exc}", err=True)
        raise typer.Exit(2) from exc


_NO_UPTIME = Uptime(day=None, week=None)


def _percent(ratio: float | None) -> str:
    """Rounded down, so 99.996% is never shown as a perfect 100%."""
    if ratio is None:
        return "-"
    value = math.floor(ratio * 10_000) / 100
    return "100%" if value == 100 else f"{value:.2f}%"


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
    """Monitor all configured targets until stopped."""
    settings = _load(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request at INFO; the Telegram request URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # As PID 1 in a container, SIGTERM would otherwise be ignored. Exiting lets asyncio clean up.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_monitor(settings))


@app.command()
def check(
    target: Annotated[str, typer.Argument(help="An http(s):// URL, host:port, or a DNS name.")],
    timeout: Annotated[float, typer.Option(help="Seconds before the check is aborted.")] = 10.0,
    ssl_warn_days: Annotated[
        int, typer.Option(help="Fail if the certificate expires sooner.")
    ] = 14,
) -> None:
    """Check one target right now. Exits with code 1 if it is down."""
    result = asyncio.run(run_check(target_from_string(target), timeout, ssl_warn_days))
    typer.echo(_format_result(result))
    raise typer.Exit(0 if result.ok else 1)


def _state_label(result: CheckResult | None, paused: bool) -> str:
    if paused:
        return "PAUSED"
    if result is None:
        return "PENDING"
    if not result.ok:
        return "DOWN"
    return "DEGRADED" if result.degraded else "UP"


@app.command()
def status(config: ConfigOption = Path("config.yaml")) -> None:
    """Show the latest result and 24h / 7d uptime of every configured target."""
    settings = _load(config)
    if not settings.database.exists():
        typer.echo(f"Базы данных {settings.database} нет: запускался ли `netpulse run`?", err=True)
        raise typer.Exit(1)

    with Storage(settings.database) as storage:
        latest = {result.target: result for result in storage.latest()}
        uptime = storage.uptime(datetime.now(UTC))
        paused_in_db = storage.paused_names()

    rows = []
    for target in sorted(settings.targets, key=lambda item: item.name):
        result = latest.get(target.name)
        windows = uptime.get(target.name, _NO_UPTIME)
        paused = target.paused or target.name in paused_in_db
        rows.append(
            (
                target.name,
                _state_label(result, paused),
                f"{result.latency_ms:.0f} ms" if result and result.latency_ms is not None else "-",
                _percent(windows.day),
                _percent(windows.week),
                result.checked_at.strftime("%Y-%m-%d %H:%M:%S") if result else "-",
                (result.error or "") if result else "",
            )
        )
    header = ("TARGET", "STATE", "LATENCY", "UPTIME 24H", "UPTIME 7D", "CHECKED (UTC)", "ERROR")
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        typer.echo(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


NameArgument = Annotated[str, typer.Argument(help="Target name as written in the config.")]


def _find_target(settings: Settings, name: str) -> Target:
    for target in settings.targets:
        if target.name == name:
            return target
    known = [target.name for target in settings.targets]
    hint = difflib.get_close_matches(name, known, n=1)
    suggestion = f" Возможно, «{hint[0]}»?" if hint else ""
    typer.echo(
        f"Ошибка: цели «{name}» нет в конфиге.{suggestion} Есть: {', '.join(known)}", err=True
    )
    raise typer.Exit(2)


@app.command()
def pause(name: NameArgument, config: ConfigOption = Path("config.yaml")) -> None:
    """Put a target into maintenance: no checks, no alerts, a grey card on the page."""
    settings = _load(config)
    target = _find_target(settings, name)
    with Storage(settings.database) as storage:
        newly_paused = storage.pause(target.name, datetime.now(UTC))
    if newly_paused:
        typer.echo(f"Цель «{name}» поставлена на обслуживание. Вернуть: netpulse resume {name}")
    else:
        typer.echo(f"Цель «{name}» уже на обслуживании.")


@app.command()
def resume(name: NameArgument, config: ConfigOption = Path("config.yaml")) -> None:
    """Take a target out of maintenance (set by `netpulse pause`) and check it again."""
    settings = _load(config)
    target = _find_target(settings, name)
    with Storage(settings.database) as storage:
        was_paused = storage.resume(target.name)
    if target.paused:
        typer.echo(
            f"Цель «{name}» остаётся на обслуживании: в конфиге у неё `paused: true`. "
            "Уберите этот параметр, изменения подхватятся без перезапуска.",
            err=True,
        )
        raise typer.Exit(1)
    if was_paused:
        typer.echo(f"Цель «{name}» снова проверяется.")
    else:
        typer.echo(f"Цель «{name}» не была на обслуживании.")


if __name__ == "__main__":
    app()
