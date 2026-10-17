"""Moteur de backtest : même stratégie, même module de risque qu'en démo et en live.

- Signal sur bougie close, exécution à l'ouverture suivante (aucun look-ahead).
- Frais taker à l'entrée et à la sortie, slippage par côté, funding historique.
- Stop touché en cours de bougie : sortie au stop (ou à l'ouverture si gap).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

import pandas as pd

from bot.data.candles import BAR
from bot.exchange.instruments import DEFAULT_RULES, InstrumentRules
from bot.models import ClosedTrade, Position, Side, Signal
from bot.risk.limits import RiskConfig
from bot.risk.manager import RiskManager, tighten_stop
from bot.strategy.base import Strategy

HISTORY_BARS = 1000  # fenêtre passée à la stratégie, identique en live
DEFAULT_SLIPPAGE = 0.0005


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity: pd.Series
    initial_equity: float
    vetoes: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    trade_returns: list[float] = field(default_factory=list)  # P&L net / equity à l'entrée


@dataclass
class _Pending:
    signal: Signal | None = None
    qty: float = 0.0
    stop: float = 0.0
    exit_reason: str = ""


def run_backtest(
    bars: dict[str, pd.DataFrame],
    strategy: Strategy,
    cfg: RiskConfig,
    *,
    initial_equity: float = 1000.0,
    funding: dict[str, pd.Series] | None = None,
    slippage: float = DEFAULT_SLIPPAGE,
    rules: dict[str, InstrumentRules] | None = None,
    trade_from: datetime | None = None,
) -> BacktestResult:
    rules = rules or DEFAULT_RULES
    funding = funding or {}
    risk = RiskManager(cfg)
    realized = initial_equity
    positions: dict[str, Position] = {}
    pending: dict[str, _Pending] = {}
    trades: list[ClosedTrade] = []
    returns: list[float] = []
    entry_equity: dict[str, float] = {}
    vetoes: dict[str, int] = {}
    curve: dict[pd.Timestamp, float] = {}
    pos_index = {s: {t: i for i, t in enumerate(df.index)} for s, df in bars.items()}
    timeline = sorted(set().union(*(df.index for df in bars.values())))

    def close(sym: str, price: float, when: datetime, reason: str) -> None:
        nonlocal realized
        p = positions.pop(sym)
        fee = p.qty * price * cfg.fee_taker
        trade = ClosedTrade(
            sym, p.side, p.qty, p.entry, price, p.initial_stop, p.opened_at, when,
            p.fees + fee, p.funding, reason, p.strategy,
        )  # fmt: skip
        trades.append(trade)
        realized += trade.net_pnl
        returns.append(trade.net_pnl / entry_equity.pop(sym))
        risk.record_trade(trade.net_pnl, when)

    for t in timeline:
        # 1) Ouverture de bougie : funding, ordres en attente, stops touchés.
        for sym, df in bars.items():
            i = pos_index[sym].get(t)
            if i is None:
                continue
            bar = df.iloc[i]
            o, h, lo = float(bar["open"]), float(bar["high"]), float(bar["low"])
            when = t.to_pydatetime()

            p = positions.get(sym)
            rate = funding.get(sym)
            if p is not None and rate is not None and t in rate.index:
                payment = p.qty * o * float(rate.loc[t]) * p.side.sign
                p.funding += payment

            order = pending.pop(sym, None)
            if order and order.exit_reason and sym in positions:
                fill = o * (1 - slippage * positions[sym].side.sign)
                close(sym, fill, when, order.exit_reason)
            elif order and order.signal and sym not in positions:
                s = order.signal
                fill = o * (1 + slippage * s.side.sign)
                if (fill - order.stop) * s.side.sign > 0:  # gap au-delà du stop : on n'entre pas
                    positions[sym] = Position(
                        sym, s.side, order.qty, fill, order.stop, order.stop, when, s.strategy,
                        fees=order.qty * fill * cfg.fee_taker,
                    )  # fmt: skip
                    entry_equity[sym] = realized

            p = positions.get(sym)
            if p is not None:
                hit = lo <= p.stop if p.side is Side.LONG else h >= p.stop
                if hit:
                    base = min(o, p.stop) if p.side is Side.LONG else max(o, p.stop)
                    reason = "stop initial" if p.stop == p.initial_stop else "stop suiveur"
                    close(sym, base * (1 - slippage * p.side.sign), when, reason)

        # 2) Clôture de bougie : equity, gestion, signaux.
        close_time = (t + BAR).to_pydatetime()
        last_close = {
            sym: float(df["close"].iloc[pos_index[sym][t]])
            for sym, df in bars.items()
            if t in pos_index[sym]
        }
        unrealized = sum(
            (last_close.get(s, p.entry) - p.entry) * p.side.sign * p.qty - p.funding
            for s, p in positions.items()
        )
        equity = realized + unrealized - sum(p.fees for p in positions.values())
        curve[t + BAR] = equity
        risk.update_equity(equity, close_time)

        for sym, df in bars.items():
            i = pos_index[sym].get(t)
            if i is None:
                continue
            window = df.iloc[max(0, i - HISTORY_BARS + 1) : i + 1]
            p = positions.get(sym)
            if p is not None:
                m = strategy.manage(p, window)
                if m.exit:
                    pending[sym] = _Pending(exit_reason=m.reason or "sortie stratégie")
                elif m.new_stop is not None:
                    tighter = tighten_stop(p, m.new_stop, last_close[sym])
                    if tighter is not None:
                        positions[sym] = replace(p, stop=tighter)
                continue
            if trade_from is not None and close_time <= trade_from:
                continue
            sig = strategy.signal(sym, window)
            if sig is None:
                continue
            d = risk.evaluate(
                sig, equity=equity, positions=positions.values(), rules=rules[sym], now=close_time
            )
            if d.approved:
                pending[sym] = _Pending(signal=sig, qty=d.qty, stop=d.stop)
            else:
                key = d.reason.split(":")[0]
                vetoes[key] = vetoes.get(key, 0) + 1

    # Positions encore ouvertes : clôturées au dernier prix pour le bilan.
    for sym in list(positions):
        df = bars[sym]
        close(
            sym,
            float(df["close"].iloc[-1]),
            (df.index[-1] + BAR).to_pydatetime(),
            "fin du backtest",
        )
    if curve:
        curve[max(curve)] = realized

    return BacktestResult(
        trades=trades,
        equity=pd.Series(curve, dtype=float).sort_index(),
        initial_equity=initial_equity,
        vetoes=vetoes,
        events=list(risk.events),
        trade_returns=returns,
    )
