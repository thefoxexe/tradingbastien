"""Stratégie v1 : suivi de tendance sur bougies 4 h (SPEC, « Stratégie de base »).

- Filtre : long si EMA 50 > EMA 200, short si EMA 50 < EMA 200.
- Entrée : clôture au-delà du plus haut (plus bas) des 20 bougies précédentes.
- Stop initial : 2 × ATR(14).
- Break-even à +1R, puis trailing à 3 × ATR(14).
- Sortie : clôture sous le plus bas (au-dessus du plus haut) des 10 bougies précédentes.

Les paramètres sont figés : pas de retouche pour faire passer un backtest.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.models import Position, Side, Signal
from bot.strategy.base import Management


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = bars["close"].shift(1)
    tr = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


@dataclass(frozen=True)
class TrendFollowing:
    name: str = "trend"
    ema_fast: int = 50
    ema_slow: int = 200
    breakout: int = 20
    atr_period: int = 14
    stop_atr: float = 2.0
    trail_atr: float = 3.0
    exit_lookback: int = 10

    @property
    def warmup(self) -> int:
        return self.ema_slow + 1

    def signal(self, symbol: str, bars: pd.DataFrame) -> Signal | None:
        if len(bars) < self.warmup:
            return None
        close = bars["close"]
        last = float(close.iloc[-1])
        fast = float(ema(close, self.ema_fast).iloc[-1])
        slow = float(ema(close, self.ema_slow).iloc[-1])
        a = float(atr(bars, self.atr_period).iloc[-1])
        prior = bars.iloc[-self.breakout - 1 : -1]
        when = bars.index[-1].to_pydatetime()
        if fast > slow and last > float(prior["high"].max()):
            return Signal(symbol, Side.LONG, last, last - self.stop_atr * a, when, self.name)
        if fast < slow and last < float(prior["low"].min()):
            return Signal(symbol, Side.SHORT, last, last + self.stop_atr * a, when, self.name)
        return None

    def manage(self, position: Position, bars: pd.DataFrame) -> Management:
        since = bars[bars.index >= pd.Timestamp(position.opened_at)]
        if since.empty or len(bars) <= self.exit_lookback:
            return Management()
        last = float(bars["close"].iloc[-1])
        prior = bars.iloc[-self.exit_lookback - 1 : -1]
        long = position.side is Side.LONG
        if (long and last < float(prior["low"].min())) or (
            not long and last > float(prior["high"].max())
        ):
            return Management(
                exit=True, reason=f"clôture au-delà de l'extrême {self.exit_lookback} bougies"
            )

        best = float(since["high"].max()) if long else float(since["low"].min())
        if position.r_multiple(best) < 1:
            return Management()
        a = float(atr(bars, self.atr_period).iloc[-1])
        trail = best - self.trail_atr * a if long else best + self.trail_atr * a
        stop = max(position.entry, trail) if long else min(position.entry, trail)
        return Management(new_stop=stop, reason="break-even / trailing")
