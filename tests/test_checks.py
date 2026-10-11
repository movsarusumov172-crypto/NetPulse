import asyncio
import socket
import ssl
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from netpulse.checks import Checker, ssl_days_left
from netpulse.models import DnsTarget, HttpTarget, TcpTarget, target_from_string

WARN_DAYS = 14

Handler = Callable[[httpx.Request], Any]
ClientFactory = Callable[[Handler], httpx.AsyncClient]


@pytest.fixture
async def client_for() -> AsyncIterator[ClientFactory]:
    clients: list[httpx.AsyncClient] = []

    def make(handler: Handler) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


def unused_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no HTTP request expected")


def http_target(expected_status: int = 200) -> HttpTarget:
    return HttpTarget(name="web", url="https://example.test", expected_status=expected_status)


async def test_http_ok_records_status_and_latency(client_for: ClientFactory) -> None:
    checker = Checker(client_for(lambda r: httpx.Response(200)), 5, WARN_DAYS)
    result = await checker.check(http_target())
    assert result.ok
    assert result.status_code == 200
    assert result.latency_ms is not None
    assert result.ssl_days_left is None  # the mock transport has no TLS stream


async def test_http_unexpected_status_fails_but_keeps_latency(client_for: ClientFactory) -> None:
    checker = Checker(client_for(lambda r: httpx.Response(503)), 5, WARN_DAYS)
    result = await checker.check(http_target())
    assert not result.ok
    assert result.status_code == 503
    assert result.latency_ms is not None
    assert result.error == "unexpected status 503 (expected 200)"


async def test_http_custom_expected_status(client_for: ClientFactory) -> None:
    checker = Checker(client_for(lambda r: httpx.Response(401)), 5, WARN_DAYS)
    assert (await checker.check(http_target(expected_status=401))).ok


async def test_http_connection_error_is_a_failed_result_not_an_exception(
    client_for: ClientFactory,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = await Checker(client_for(refuse), 5, WARN_DAYS).check(http_target())
    assert not result.ok
    assert result.error == "ConnectError: connection refused"
    assert result.latency_ms is None


async def test_slow_http_response_times_out(client_for: ClientFactory) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    result = await Checker(client_for(slow), 0.01, WARN_DAYS).check(http_target())
    assert not result.ok
    assert result.error is not None
    assert "timed out" in result.error


class FakeSslObject:
    def __init__(self, cert: dict[str, Any]) -> None:
        self._cert = cert

    def getpeercert(self) -> dict[str, Any]:
        return self._cert


def test_ssl_days_left_parses_certificate_expiry() -> None:
    expires = time.time() + 40 * 86_400 + 3600
    not_after = time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(expires))
    assert ssl.cert_time_to_seconds(not_after) > time.time()
    assert ssl_days_left(FakeSslObject({"notAfter": not_after})) == 40


def test_ssl_days_left_is_none_without_certificate() -> None:
    assert ssl_days_left(None) is None
    assert ssl_days_left(FakeSslObject({})) is None


class FakeStream:
    def get_extra_info(self, name: str) -> object:
        assert name == "ssl_object"
        return object()


@pytest.mark.parametrize(("days_left", "ok"), [(30, True), (14, True), (13, False), (-2, False)])
async def test_http_fails_when_certificate_expires_soon(
    client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch, days_left: int, ok: bool
) -> None:
    monkeypatch.setattr("netpulse.checks.ssl_days_left", lambda _: days_left)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, extensions={"network_stream": FakeStream()})

    result = await Checker(client_for(handler), 5, WARN_DAYS).check(http_target())
    assert result.ok is ok
    assert result.ssl_days_left == days_left
    assert result.status_code == 200
    if not ok:
        assert result.error == f"SSL certificate expires in {days_left} days"


class FakeWriter:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


DB = TcpTarget(name="db", host="db.test", port=5432)


async def test_tcp_ok(client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    async def open_connection(host: str, port: int) -> tuple[None, FakeWriter]:
        assert (host, port) == ("db.test", 5432)
        return None, FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = await Checker(client_for(unused_handler), 5, WARN_DAYS).check(DB)
    assert result.ok
    assert result.latency_ms is not None


async def test_tcp_refused(client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    async def open_connection(host: str, port: int) -> None:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = await Checker(client_for(unused_handler), 5, WARN_DAYS).check(DB)
    assert not result.ok
    assert result.error == "ConnectionRefusedError: refused"


async def test_dns_ok(client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    async def getaddrinfo(host: str, port: None) -> list[object]:
        return [object()]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    result = await Checker(client_for(unused_handler), 5, WARN_DAYS).check(
        DnsTarget(name="dns", host="a.test")
    )
    assert result.ok


async def test_dns_failure(client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    async def getaddrinfo(host: str, port: None) -> None:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    result = await Checker(client_for(unused_handler), 5, WARN_DAYS).check(
        DnsTarget(name="dns", host="nope.test")
    )
    assert not result.ok
    assert result.error is not None
    assert "gaierror" in result.error


@pytest.mark.parametrize(
    ("value", "expected_type", "address"),
    [
        ("https://example.com/health", HttpTarget, "https://example.com/health"),
        ("db.internal:5432", TcpTarget, "db.internal:5432"),
        ("example.com", DnsTarget, "example.com"),
    ],
)
def test_target_from_string(value: str, expected_type: type, address: str) -> None:
    target = target_from_string(value)
    assert isinstance(target, expected_type)
    assert target.address == address


class ChunkedBody(httpx.AsyncByteStream):
    """A response body delivered in the given chunks, to exercise matches across boundaries."""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


async def test_http_sends_the_configured_method_and_headers(client_for: ClientFactory) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    target = HttpTarget(
        name="api",
        url="https://api.test/health",
        method="post",
        headers={"Authorization": "Bearer abc", "X-Env": "prod"},
        expected_status=204,
    )

    result = await Checker(client_for(handler), 5, WARN_DAYS).check(target)

    assert result.ok
    assert seen[0].method == "POST"
    assert seen[0].headers["Authorization"] == "Bearer abc"
    assert seen[0].headers["X-Env"] == "prod"


async def test_target_timeout_overrides_the_default(client_for: ClientFactory) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200)

    target = HttpTarget(name="web", url="https://example.test", timeout=0.01)

    result = await Checker(client_for(slow), 30, WARN_DAYS).check(target)

    assert not result.ok
    assert result.error == "timed out after 0.01s"


async def test_target_timeout_can_also_be_longer_than_the_default(
    client_for: ClientFactory,
) -> None:
    async def slowish(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    target = HttpTarget(name="web", url="https://example.test", timeout=5)

    assert (await Checker(client_for(slowish), 0.01, WARN_DAYS).check(target)).ok


def body_target(needle: str) -> HttpTarget:
    return HttpTarget(name="web", url="https://example.test", body_contains=needle)


async def test_expected_text_present_in_the_body(client_for: ClientFactory) -> None:
    checker = Checker(
        client_for(lambda r: httpx.Response(200, text="<b>All systems OK</b>")), 5, 14
    )

    assert (await checker.check(body_target("systems OK"))).ok


async def test_expected_text_missing_from_the_body_fails_the_check(
    client_for: ClientFactory,
) -> None:
    checker = Checker(client_for(lambda r: httpx.Response(200, text="maintenance page")), 5, 14)

    result = await checker.check(body_target("All systems OK"))

    assert not result.ok
    assert result.status_code == 200
    assert result.error == "response body does not contain 'All systems OK'"


async def test_expected_text_split_across_chunks_is_still_found(client_for: ClientFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedBody(b"..stat", b"us: h", b"ealthy.."))

    assert (await Checker(client_for(handler), 5, 14).check(body_target("status: healthy"))).ok


async def test_expected_text_is_matched_in_the_response_charset(client_for: ClientFactory) -> None:
    body = "Сервис работает".encode("windows-1251")
    headers = {"content-type": "text/html; charset=windows-1251"}
    checker = Checker(
        client_for(lambda r: httpx.Response(200, content=body, headers=headers)), 5, 14
    )

    assert (await checker.check(body_target("работает"))).ok


async def test_expected_text_beyond_the_read_limit_is_not_found(
    client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netpulse.checks._MAX_BODY_CHARS", 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedBody(b"x" * 20, b"needle"))

    assert not (await Checker(client_for(handler), 5, 14).check(body_target("needle"))).ok


async def test_wrong_status_is_reported_before_the_body_is_considered(
    client_for: ClientFactory,
) -> None:
    checker = Checker(client_for(lambda r: httpx.Response(500, text="All systems OK")), 5, 14)

    result = await checker.check(body_target("All systems OK"))

    assert result.error == "unexpected status 500 (expected 200)"


def test_head_requests_cannot_expect_body_text() -> None:
    with pytest.raises(ValueError, match="HEAD"):
        HttpTarget(name="x", url="https://example.test", method="HEAD", body_contains="ok")


async def test_configure_changes_the_default_timeout_for_later_checks(
    client_for: ClientFactory,
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    checker = Checker(client_for(slow), 5, WARN_DAYS)
    assert (await checker.check(http_target())).ok

    checker.configure(timeout=0.01, ssl_warn_days=WARN_DAYS)

    result = await checker.check(http_target())
    assert result.error == "timed out after 0.01s"
