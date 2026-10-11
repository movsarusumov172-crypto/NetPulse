import json
from datetime import timedelta

import httpx
import pytest

from netpulse.models import CheckResult, HttpTarget
from netpulse.notifier import TelegramNotifier, format_alert, format_duration
from netpulse.state import Event

TARGET = HttpTarget(name="API", url="https://api.example.com")


def test_down_alert_contains_name_address_and_error() -> None:
    result = CheckResult("API", ok=False, error="timed out after 10s")
    text = format_alert(TARGET, result, Event.WENT_DOWN)
    assert "API is DOWN" in text
    assert "https://api.example.com" in text
    assert "timed out after 10s" in text


def test_recovery_alert_contains_latency() -> None:
    text = format_alert(TARGET, CheckResult("API", ok=True, latency_ms=87.4), Event.RECOVERED)
    assert "API is back UP" in text
    assert "87 ms" in text


async def test_send_posts_to_bot_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await TelegramNotifier(client, "TOKEN", "42").send("hello")

    assert str(seen[0].url) == "https://api.telegram.org/botTOKEN/sendMessage"
    assert json.loads(seen[0].content) == {"chat_id": "42", "text": "hello"}


async def test_delivery_failure_is_swallowed_and_does_not_leak_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await TelegramNotifier(client, "SECRET-TOKEN", "42").send("hello")

    assert "not delivered" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


async def test_http_error_status_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    async with httpx.AsyncClient(transport=transport) as client:
        await TelegramNotifier(client, "SECRET-TOKEN", "42").send("hello")

    assert "HTTP 401" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (59, "59s"),
        (61, "1m 1s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (3 * 3600 + 25 * 60 + 59, "3h 25m"),
        (86_400, "1d 0h"),
        (90_000, "1d 1h"),
        (-5, "0s"),
    ],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert format_duration(timedelta(seconds=seconds)) == expected


def test_recovery_alert_reports_the_downtime() -> None:
    result = CheckResult("API", ok=True, latency_ms=50.0)
    text = format_alert(TARGET, result, Event.RECOVERED, downtime=timedelta(minutes=5, seconds=12))
    assert "back UP" in text
    assert "Downtime: 5m 12s" in text


def test_recovery_alert_without_a_known_incident_has_no_downtime_line() -> None:
    text = format_alert(TARGET, CheckResult("API", ok=True), Event.RECOVERED)
    assert "Downtime" not in text
