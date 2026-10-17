"""Métriques, Monte Carlo, walk-forward et critères go/no-go."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from bot.backtest.engine import DEFAULT_SLIPPAGE, BacktestResult, run_backtest
from bot.models import ClosedTrade
from bot.risk.limits import RiskConfig
from bot.strategy.base import Strategy

# Critères de passage backtest → démo (SPEC, « Backtest et critères go/no-go »).
MIN_TRADES = 100
MIN_PROFIT_FACTOR = 1.3
MAX_BACKTEST_DRAWDOWN = 0.20
MIN_YEARS = 3.0


@dataclass(frozen=True)
class Metrics:
    trades: int
    win_rate: float
    profit_factor: float
    avg_r: float
    net_pnl: float
    total_return: float
    max_drawdown: float
    fees: float
    funding: float
    years: float

    def as_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


def max_drawdown(equity: pd.Series | Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for v in equity:
        peak = max(peak, float(v))
        if peak > 0:
            worst = max(worst, 1 - float(v) / peak)
    return worst


def profit_factor(trades: Sequence[ClosedTrade]) -> float:
    gains = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    losses = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def compute_metrics(result: BacktestResult) -> Metrics:
    trades = result.trades
    eq = result.equity
    final = float(eq.iloc[-1]) if len(eq) else result.initial_equity
    years = (eq.index[-1] - eq.index[0]).days / 365.25 if len(eq) > 1 else 0.0
    return Metrics(
        trades=len(trades),
        win_rate=sum(t.net_pnl > 0 for t in trades) / len(trades) if trades else 0.0,
        profit_factor=profit_factor(trades),
        avg_r=sum(t.r_multiple for t in trades) / len(trades) if trades else 0.0,
        net_pnl=sum(t.net_pnl for t in trades),
        total_return=final / result.initial_equity - 1,
        max_drawdown=max_drawdown(eq),
        fees=sum(t.fees for t in trades),
        funding=sum(t.funding for t in trades),
        years=years,
    )


def monte_carlo_drawdown(
    returns: Sequence[float], runs: int = 2000, seed: int = 7
) -> dict[str, float]:
    """Pire drawdown probable en mélangeant l'ordre des trades (rendements composés)."""
    if not returns:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    rng = random.Random(seed)
    sample = list(returns)
    dds: list[float] = []
    for _ in range(runs):
        rng.shuffle(sample)
        eq, curve = 1.0, [1.0]
        for r in sample:
            eq *= 1 + r
            curve.append(eq)
        dds.append(max_drawdown(curve))
    dds.sort()

    def pct(q: float) -> float:
        return dds[min(len(dds) - 1, int(q * len(dds)))]

    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


@dataclass(frozen=True)
class WalkForward:
    split: datetime
    in_sample: Metrics
    out_of_sample: Metrics
    oos_result: BacktestResult


def walk_forward(
    bars: dict[str, pd.DataFrame],
    strategy: Strategy,
    cfg: RiskConfig,
    *,
    ratio: float = 0.7,
    initial_equity: float = 1000.0,
    funding: dict[str, pd.Series] | None = None,
    slippage: float = DEFAULT_SLIPPAGE,
) -> WalkForward:
    """70 % pour l'échantillon de réglage, 30 % jamais vus pour la validation.

    Les paramètres de la stratégie sont figés : la partie hors échantillon
    reçoit l'historique antérieur uniquement pour chauffer les indicateurs.
    """
    start = min(df.index[0] for df in bars.values())
    end = max(df.index[-1] for df in bars.values())
    split = (start + (end - start) * ratio).to_pydatetime()
    ins = {s: df[df.index < split] for s, df in bars.items()}
    ins_res = run_backtest(
        ins, strategy, cfg, initial_equity=initial_equity, funding=funding, slippage=slippage
    )
    oos_res = run_backtest(
        bars,
        strategy,
        cfg,
        initial_equity=initial_equity,
        funding=funding,
        slippage=slippage,
        trade_from=split,
    )
    oos_res.equity = oos_res.equity[oos_res.equity.index >= split]
    return WalkForward(split, compute_metrics(ins_res), compute_metrics(oos_res), oos_res)


@dataclass(frozen=True)
class Criterion:
    name: str
    value: str
    passed: bool


def go_no_go(full: Metrics, oos: Metrics) -> list[Criterion]:
    return [
        Criterion("≥ 3 ans de données", f"{full.years:.1f} ans", full.years >= MIN_YEARS - 0.05),
        Criterion(f"≥ {MIN_TRADES} trades", str(full.trades), full.trades >= MIN_TRADES),
        Criterion(
            f"Profit factor ≥ {MIN_PROFIT_FACTOR} après frais",
            f"{full.profit_factor:.2f}",
            full.profit_factor >= MIN_PROFIT_FACTOR,
        ),
        Criterion(
            f"Drawdown max ≤ {MAX_BACKTEST_DRAWDOWN:.0%}",
            f"{full.max_drawdown:.1%}",
            full.max_drawdown <= MAX_BACKTEST_DRAWDOWN,
        ),
        Criterion("Résultat positif hors échantillon", f"{oos.net_pnl:+.2f} USDT", oos.net_pnl > 0),
    ]
