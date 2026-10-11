"""Single-shot health checks for HTTP, TCP and DNS targets."""

import asyncio
import ssl
import time
from typing import Any

import httpx

from netpulse.models import CheckResult, DnsTarget, HttpTarget, Target, TcpTarget

_SECONDS_PER_DAY = 86_400


def ssl_days_left(ssl_object: Any) -> int | None:
    """Whole days until the peer certificate expires, or None if there is no certificate."""
    cert = ssl_object.getpeercert() if ssl_object is not None else None
    if not cert or "notAfter" not in cert:
        return None
    expires_at = ssl.cert_time_to_seconds(cert["notAfter"])
    return int((expires_at - time.time()) // _SECONDS_PER_DAY)


class Checker:
    """Runs one check attempt against a target. Never raises on network failures."""

    def __init__(self, client: httpx.AsyncClient, timeout: float, ssl_warn_days: int) -> None:
        self._client = client
        self._timeout = timeout
        self._ssl_warn_days = ssl_warn_days

    async def check(self, target: Target) -> CheckResult:
        try:
            async with asyncio.timeout(self._timeout):
                match target:
                    case HttpTarget():
                        return await self._check_http(target)
                    case TcpTarget():
                        return await self._check_tcp(target)
                    case DnsTarget():
                        return await self._check_dns(target)
        except TimeoutError:
            return CheckResult(target.name, ok=False, error=f"timed out after {self._timeout:g}s")
        except (httpx.HTTPError, OSError) as exc:
            return CheckResult(target.name, ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _check_http(self, target: HttpTarget) -> CheckResult:
        started = time.perf_counter()
        # Stream so the body is never downloaded: latency is time to response headers.
        async with self._client.stream("GET", str(target.url)) as response:
            latency_ms = (time.perf_counter() - started) * 1000
            stream = response.extensions.get("network_stream")
            days_left = ssl_days_left(stream.get_extra_info("ssl_object")) if stream else None

        error = None
        if response.status_code != target.expected_status:
            error = f"unexpected status {response.status_code} (expected {target.expected_status})"
        elif days_left is not None and days_left < self._ssl_warn_days:
            error = f"SSL certificate expires in {days_left} days"
        return CheckResult(
            target.name,
            ok=error is None,
            latency_ms=latency_ms,
            status_code=response.status_code,
            ssl_days_left=days_left,
            error=error,
        )

    async def _check_tcp(self, target: TcpTarget) -> CheckResult:
        started = time.perf_counter()
        _, writer = await asyncio.open_connection(target.host, target.port)
        latency_ms = (time.perf_counter() - started) * 1000
        writer.close()
        await writer.wait_closed()
        return CheckResult(target.name, ok=True, latency_ms=latency_ms)

    async def _check_dns(self, target: DnsTarget) -> CheckResult:
        started = time.perf_counter()
        await asyncio.get_running_loop().getaddrinfo(target.host, None)
        latency_ms = (time.perf_counter() - started) * 1000
        return CheckResult(target.name, ok=True, latency_ms=latency_ms)


async def run_check(target: Target, timeout: float, ssl_warn_days: int) -> CheckResult:
    """One-off check with a throwaway client, used by the `check` CLI command."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
        return await Checker(client, timeout, ssl_warn_days).check(target)
