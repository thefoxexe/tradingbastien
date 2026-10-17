"""Client Bybit V5 : fine couche typée au-dessus de pybit, démo ou live.

- Retry avec backoff exponentiel sur erreurs réseau, rate limit et horloge.
- Aucune méthode de retrait ou de transfert n'existe ici, volontairement.
- Les ordres d'entrée portent toujours ``stopLoss`` (``slTriggerBy=MarkPrice``,
  ``tpslMode=Full``) : il n'y a pas d'autre façon d'ouvrir une position.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from bot.config import Mode, Settings
from bot.exchange.instruments import InstrumentRules
from bot.models import Side

log = logging.getLogger(__name__)

CATEGORY = "linear"
RETRYABLE_CODES = frozenset({10000, 10002, 10006, 10016})  # timeout, horloge, rate limit, serveur
DUPLICATE_LINK_ID = 110072


class ExchangeError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class Session(Protocol):
    """Sous-ensemble de ``pybit.unified_trading.HTTP`` utilisé par le bot."""

    def get_server_time(self) -> dict[str, Any]: ...
    def get_api_key_information(self, **kw: Any) -> dict[str, Any]: ...
    def get_instruments_info(self, **kw: Any) -> dict[str, Any]: ...
    def get_kline(self, **kw: Any) -> dict[str, Any]: ...
    def get_funding_rate_history(self, **kw: Any) -> dict[str, Any]: ...
    def get_tickers(self, **kw: Any) -> dict[str, Any]: ...
    def get_wallet_balance(self, **kw: Any) -> dict[str, Any]: ...
    def get_positions(self, **kw: Any) -> dict[str, Any]: ...
    def get_open_orders(self, **kw: Any) -> dict[str, Any]: ...
    def get_executions(self, **kw: Any) -> dict[str, Any]: ...
    def get_closed_pnl(self, **kw: Any) -> dict[str, Any]: ...
    def place_order(self, **kw: Any) -> dict[str, Any]: ...
    def cancel_all_orders(self, **kw: Any) -> dict[str, Any]: ...
    def set_trading_stop(self, **kw: Any) -> dict[str, Any]: ...
    def set_leverage(self, **kw: Any) -> dict[str, Any]: ...
    def switch_margin_mode(self, **kw: Any) -> dict[str, Any]: ...
    def switch_position_mode(self, **kw: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExchangePosition:
    symbol: str
    side: Side
    qty: float
    entry: float
    stop: float | None
    liq_price: float | None
    mark_price: float
    unrealized_pnl: float
    leverage: float
    created_at: datetime


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out != 0 else None


def make_session(settings: Settings) -> Session:
    """Session pybit pour le mode courant. La démo passe par ``api-demo.bybit.com``."""
    if settings.mode is Mode.BACKTEST:
        raise ExchangeError("Aucune session Bybit authentifiée en mode backtest.")
    from pybit.unified_trading import HTTP  # noqa: PLC0415 - import paresseux

    session: Session = HTTP(
        testnet=False,
        demo=settings.mode is Mode.DEMO,
        api_key=settings.bybit_api_key.get_secret_value(),
        api_secret=settings.bybit_api_secret.get_secret_value(),
        recv_window=settings.recv_window_ms,
        max_retries=0,
    )
    return session


class BybitClient:
    def __init__(
        self,
        session: Session,
        *,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._s = session
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep

    # ------------------------------------------------------------------ appels
    def _call(self, fn: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        delay = self._base_delay
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = fn(**kwargs)
            except Exception as exc:  # pybit : FailedRequestError / InvalidRequestError
                code = getattr(exc, "status_code", None)
                code = int(code) if isinstance(code, int | str) and str(code).isdigit() else None
                retryable = code is None or code in RETRYABLE_CODES or 500 <= code < 600
                if code == DUPLICATE_LINK_ID or not retryable or attempt == self._max_attempts:
                    raise ExchangeError(f"{fn.__name__}: {exc}", code) from exc
                log.warning("retry bybit", extra={"call": fn.__name__, "attempt": attempt})
                self._sleep(delay)
                delay *= 2
                continue
            code = int(resp.get("retCode", 0))
            if code == 0:
                result: dict[str, Any] = resp.get("result") or {}
                return result
            if code in RETRYABLE_CODES and attempt < self._max_attempts:
                self._sleep(delay)
                delay *= 2
                continue
            raise ExchangeError(f"{fn.__name__}: {resp.get('retMsg')}", code)
        raise ExchangeError(
            f"{fn.__name__}: échec après {self._max_attempts} essais"
        )  # pragma: no cover

    # ------------------------------------------------------------------ compte
    def server_time(self) -> datetime:
        res = self._call(self._s.get_server_time)
        return datetime.fromtimestamp(int(res["timeNano"]) / 1e9, UTC)

    def api_key_info(self) -> dict[str, Any]:
        return self._call(self._s.get_api_key_information)

    def equity(self) -> float:
        res = self._call(self._s.get_wallet_balance, accountType="UNIFIED", coin="USDT")
        accounts = res.get("list") or []
        if not accounts:
            raise ExchangeError("Solde introuvable")
        acc = accounts[0]
        value = acc.get("totalEquity")
        if not value:
            coins = [c for c in acc.get("coin", []) if c.get("coin") == "USDT"]
            value = coins[0].get("equity") if coins else None
        if value in (None, ""):
            raise ExchangeError("Equity illisible")
        return float(value)

    def setup_symbol(self, symbol: str, leverage: int) -> None:
        """Marge isolée, mode one-way, levier Bybit fixé (≤ plafond)."""
        for fn, kw in (
            (self._s.switch_position_mode, {"symbol": symbol, "mode": 0}),
            (
                self._s.switch_margin_mode,
                {
                    "symbol": symbol,
                    "tradeMode": 1,
                    "buyLeverage": str(leverage),
                    "sellLeverage": str(leverage),
                },
            ),
            (
                self._s.set_leverage,
                {"symbol": symbol, "buyLeverage": str(leverage), "sellLeverage": str(leverage)},
            ),
        ):
            try:
                self._call(fn, category=CATEGORY, **kw)
            except ExchangeError as exc:
                # 110025/110026/110043 : déjà dans l'état voulu.
                if exc.code not in (110025, 110026, 110043):
                    raise

    # ------------------------------------------------------------------ marché
    def instruments(self, symbols: list[str]) -> dict[str, InstrumentRules]:
        out: dict[str, InstrumentRules] = {}
        for symbol in symbols:
            res = self._call(self._s.get_instruments_info, category=CATEGORY, symbol=symbol)
            items = res.get("list") or []
            if not items:
                raise ExchangeError(f"Instrument inconnu : {symbol}")
            out[symbol] = InstrumentRules.from_bybit(items[0])
        return out

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[list[str]]:
        """Bougies dans l'ordre chronologique (Bybit les renvoie du plus récent au plus ancien)."""
        kw: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_ms is not None:
            kw["start"] = start_ms
        if end_ms is not None:
            kw["end"] = end_ms
        rows: list[list[str]] = self._call(self._s.get_kline, **kw).get("list") or []
        return sorted(rows, key=lambda r: int(r[0]))

    def funding_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[dict[str, str]]:
        kw: dict[str, Any] = {"category": CATEGORY, "symbol": symbol, "limit": 200}
        if start_ms is not None:
            kw["startTime"] = start_ms
        if end_ms is not None:
            kw["endTime"] = end_ms
        rows: list[dict[str, str]] = (
            self._call(self._s.get_funding_rate_history, **kw).get("list") or []
        )
        return sorted(rows, key=lambda r: int(r["fundingRateTimestamp"]))

    def mark_price(self, symbol: str) -> float:
        items = self._call(self._s.get_tickers, category=CATEGORY, symbol=symbol).get("list") or []
        if not items:
            raise ExchangeError(f"Ticker introuvable : {symbol}")
        return float(items[0]["markPrice"])

    # ------------------------------------------------------------------ positions et ordres
    def positions(self) -> list[ExchangePosition]:
        res = self._call(self._s.get_positions, category=CATEGORY, settleCoin="USDT")
        out: list[ExchangePosition] = []
        for p in res.get("list") or []:
            size = float(p.get("size") or 0)
            if size == 0:
                continue
            out.append(
                ExchangePosition(
                    symbol=p["symbol"],
                    side=Side.LONG if p["side"] == "Buy" else Side.SHORT,
                    qty=size,
                    entry=float(p["avgPrice"]),
                    stop=_f(p.get("stopLoss")),
                    liq_price=_f(p.get("liqPrice")),
                    mark_price=float(p.get("markPrice") or 0),
                    unrealized_pnl=float(p.get("unrealisedPnl") or 0),
                    leverage=float(p.get("leverage") or 0),
                    created_at=datetime.fromtimestamp(int(p.get("createdTime") or 0) / 1000, UTC),
                )
            )
        return out

    def open_orders(self) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = (
            self._call(self._s.get_open_orders, category=CATEGORY, settleCoin="USDT").get("list")
            or []
        )
        return orders

    def executions(self, symbol: str, order_link_id: str | None = None) -> list[dict[str, Any]]:
        kw: dict[str, Any] = {"category": CATEGORY, "symbol": symbol}
        if order_link_id:
            kw["orderLinkId"] = order_link_id
        rows: list[dict[str, Any]] = self._call(self._s.get_executions, **kw).get("list") or []
        return rows

    def closed_pnl(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = (
            self._call(self._s.get_closed_pnl, category=CATEGORY, symbol=symbol, limit=limit).get(
                "list"
            )
            or []
        )
        return rows

    def open_with_stop(
        self, symbol: str, side: Side, qty: Decimal, stop: Decimal, link_id: str
    ) -> str:
        """Entrée au marché AVEC stop-loss attaché dans le même ordre (règle 1)."""
        if stop <= 0 or qty <= 0:
            raise ExchangeError("Ordre refusé : stop ou quantité invalide")
        try:
            res = self._call(
                self._s.place_order,
                category=CATEGORY,
                symbol=symbol,
                side=side.bybit_entry,
                orderType="Market",
                qty=str(qty),
                stopLoss=str(stop),
                slTriggerBy="MarkPrice",
                tpslMode="Full",
                slOrderType="Market",
                positionIdx=0,
                orderLinkId=link_id,
            )
        except ExchangeError as exc:
            if exc.code == DUPLICATE_LINK_ID:
                log.warning("ordre déjà placé (orderLinkId en double)", extra={"link_id": link_id})
                return link_id
            raise
        return str(res.get("orderId") or link_id)

    def close_market(self, symbol: str, side: Side, qty: float) -> None:
        """Clôture au marché d'une position (reduceOnly)."""
        self._call(
            self._s.place_order,
            category=CATEGORY,
            symbol=symbol,
            side=side.bybit_exit,
            orderType="Market",
            qty=str(qty),
            reduceOnly=True,
            positionIdx=0,
        )

    def set_stop(self, symbol: str, stop: Decimal) -> None:
        self._call(
            self._s.set_trading_stop,
            category=CATEGORY,
            symbol=symbol,
            stopLoss=str(stop),
            slTriggerBy="MarkPrice",
            tpslMode="Full",
            positionIdx=0,
        )

    def cancel_all(self) -> None:
        self._call(self._s.cancel_all_orders, category=CATEGORY, settleCoin="USDT")
