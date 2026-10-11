"""Доставка алертов."""

import logging
import os
from typing import Protocol

import httpx

from netpulse.models import CheckResult, Target
from netpulse.state import Event

logger = logging.getLogger(__name__)

TOKEN_ENV = "NETPULSE_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "NETPULSE_TELEGRAM_CHAT_ID"


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


def format_alert(target: Target, result: CheckResult, event: Event) -> str:
    if event is Event.WENT_DOWN:
        return f"\N{LARGE RED CIRCLE} {target.name} is DOWN\n{target.address}\n{result.error}"
    latency = f" ({result.latency_ms:.0f} ms)" if result.latency_ms is not None else ""
    return f"\N{LARGE GREEN CIRCLE} {target.name} is back UP{latency}\n{target.address}"


class TelegramNotifier:
    """Шлёт сообщения через Telegram Bot API. Ошибки доставки логируются, а не пробрасываются."""

    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str) -> None:
        self._client = client
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        # Логируем только тип исключения / статус: URL запроса содержит токен бота.
        try:
            response = await self._client.post(
                self._url, json={"chat_id": self._chat_id, "text": text}
            )
        except httpx.HTTPError as exc:
            logger.error("Telegram alert not delivered: %s", type(exc).__name__)
            return
        if not response.is_success:
            logger.error("Telegram alert not delivered: HTTP %s", response.status_code)


def telegram_from_env(client: httpx.AsyncClient) -> TelegramNotifier | None:
    token = os.environ.get(TOKEN_ENV)
    chat_id = os.environ.get(CHAT_ID_ENV)
    if not token or not chat_id:
        logger.warning("%s / %s not set: Telegram alerts are disabled", TOKEN_ENV, CHAT_ID_ENV)
        return None
    return TelegramNotifier(client, token, chat_id)
