"""Commandes Telegram (``/status``, ``/pause``, ``/kill``) et surveillance du bot.

Seul le chat configuré (``TELEGRAM_CHAT_ID``) est écouté. ``/kill`` demande
une confirmation (``/kill CONFIRMER`` dans les 60 s), comme le double clic
du dashboard. Le watchdog alerte si le bot ne publie plus rien depuis 5 min.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from bot.config import Settings
from bot.store import Store

log = logging.getLogger(__name__)
KILL_CONFIRM_WINDOW = timedelta(seconds=60)


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._token = settings.telegram_bot_token.get_secret_value()
        self._chat = str(settings.telegram_chat_id)
        self._store = store
        self._http = client or httpx.Client(timeout=40)
        self._clock = clock
        self._offset = 0
        self._kill_requested_at: datetime | None = None

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def send(self, text: str) -> None:
        try:
            self._http.post(
                self._url("sendMessage"), json={"chat_id": self._chat, "text": text}
            ).raise_for_status()
        except httpx.HTTPError as exc:
            log.error("échec d'envoi Telegram", extra={"error": type(exc).__name__})

    def handle(self, text: str) -> str:
        cmd = text.strip().split()
        name = cmd[0].split("@")[0].lower() if cmd else ""
        now = self._clock()
        if name == "/status":
            snap = self._store.get("snapshot") or {}
            return (
                f"Statut {snap.get('status', '?')} ({snap.get('mode', '?')}) — equity "
                f"{snap.get('equity') or 0:,.2f} USDT — jour {snap.get('daily_pnl_pct', 0):+.2%} — "
                f"drawdown {snap.get('drawdown', 0):.2%}"
            )
        if name == "/pause":
            self._store.push_command("pause", "telegram")
            return "Pause demandée : plus aucune entrée, positions et stops conservés."
        if name == "/kill":
            if len(cmd) > 1 and cmd[1].upper() == "CONFIRMER":
                if self._kill_requested_at and now - self._kill_requested_at <= KILL_CONFIRM_WINDOW:
                    self._kill_requested_at = None
                    self._store.push_command("kill", "telegram")
                    return "KILL SWITCH envoyé : annulation des ordres, clôture des positions, mode HALT."
                return "Aucune demande de kill en attente. Envoyez d'abord /kill."
            self._kill_requested_at = now
            return "Confirmez avec « /kill CONFIRMER » dans les 60 s."
        return "Commandes : /status, /pause, /kill"

    def poll_once(self) -> None:
        resp = self._http.get(
            self._url("getUpdates"), params={"offset": self._offset, "timeout": 30}
        )
        resp.raise_for_status()
        for update in resp.json().get("result", []):
            self._offset = int(update["update_id"]) + 1
            msg: dict[str, Any] = update.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self._chat or not msg.get("text"):
                continue  # chat inconnu : ignoré
            self.send(self.handle(str(msg["text"])))

    def poll_forever(self, stop: threading.Event) -> None:  # pragma: no cover - réseau
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                log.warning("polling Telegram", extra={"error": type(exc).__name__})
                time.sleep(5)


def check_silence(
    store: Store, now: datetime, silent_after: timedelta = timedelta(minutes=5)
) -> bool:
    snap = store.get("snapshot") or {}
    if not snap.get("ts"):
        return True
    return now - datetime.fromisoformat(snap["ts"]) > silent_after


def watchdog(
    tg: TelegramBot, store: Store, stop: threading.Event
) -> None:  # pragma: no cover - boucle
    alerted = False
    while not stop.wait(30):
        silent = check_silence(store, datetime.now(UTC))
        if silent and not alerted:
            tg.send("ALERTE : le bot est silencieux depuis plus de 5 min.")
        elif alerted and not silent:
            tg.send("Le bot publie de nouveau son état.")
        alerted = silent
