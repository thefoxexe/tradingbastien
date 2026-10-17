"""Flux WebSocket public (mainnet, aussi en démo : les prix sont ceux du mainnet).

pybit gère la reconnexion avec backoff. Ce module ne fait que suivre la
fraîcheur des données : plus de 60 s sans message = aucune nouvelle entrée.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

STALE_AFTER_S = 60.0


class MarketStream:
    def __init__(self, symbols: list[str], clock: Callable[[], float] = time.monotonic) -> None:
        self.symbols = symbols
        self._clock = clock
        self._lock = threading.Lock()
        self._last_msg: float | None = None
        self._last_price: dict[str, float] = {}
        self._ws: Any = None

    def start(self) -> None:  # pragma: no cover - réseau
        from pybit.unified_trading import WebSocket  # noqa: PLC0415

        self._ws = WebSocket(testnet=False, channel_type="linear", ping_interval=20, retries=0)
        self._ws.ticker_stream(symbol=self.symbols, callback=self.on_message)

    def stop(self) -> None:  # pragma: no cover - réseau
        if self._ws is not None:
            self._ws.exit()

    def on_message(self, msg: dict[str, Any]) -> None:
        data = msg.get("data") or {}
        symbol = data.get("symbol")
        price = data.get("markPrice") or data.get("lastPrice")
        with self._lock:
            self._last_msg = self._clock()
            if symbol and price:
                self._last_price[str(symbol)] = float(price)

    def seconds_since_last(self) -> float | None:
        with self._lock:
            return None if self._last_msg is None else self._clock() - self._last_msg

    def is_stale(self) -> bool:
        age = self.seconds_since_last()
        return age is None or age > STALE_AFTER_S

    def price(self, symbol: str) -> float | None:
        with self._lock:
            return self._last_price.get(symbol)
