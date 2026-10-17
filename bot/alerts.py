"""Alertes : journal en base + Telegram (si configuré). Jamais de secret dans un message."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from bot.config import Settings

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, text: str, *, level: str = "INFO", kind: str = "info") -> None: ...


class EventSink(Protocol):
    def log_event(self, kind: str, message: str, level: str = "INFO") -> None: ...


class TelegramNotifier:
    def __init__(
        self, settings: Settings, sink: EventSink | None = None, client: httpx.Client | None = None
    ) -> None:
        self._token = settings.telegram_bot_token.get_secret_value()
        self._chat = settings.telegram_chat_id
        self._mode = settings.mode.value.upper()
        self._sink = sink
        self._http = client or httpx.Client(timeout=10)

    def send(self, text: str, *, level: str = "INFO", kind: str = "info") -> None:
        log.log(logging.getLevelName(level), text, extra={"kind": kind})
        if self._sink is not None:
            try:
                self._sink.log_event(kind, text, level)
            except Exception:
                log.exception("journal indisponible")
        if not (self._token and self._chat):
            return
        try:
            self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat, "text": f"[{self._mode}] {text}"},
            ).raise_for_status()
        except httpx.HTTPError as exc:
            # L'URL contient le token : on ne logge que le type d'erreur.
            log.error("échec d'envoi Telegram", extra={"error": type(exc).__name__})
