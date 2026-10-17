from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bot.backtest.__main__ import download, main, run
from bot.backtest.engine import run_backtest
from bot.backtest.metrics import (
    compute_metrics,
    go_no_go,
    max_drawdown,
    monte_carlo_drawdown,
    profit_factor,
    walk_forward,
)
from bot.backtest.report import render_report
from bot.data.candles import (
    BAR,
    closed_bars,
    download_funding,
    download_klines,
    find_gaps,
    is_valid,
    load_candles,
    load_funding,
    rows_to_frame,
    save_csv,
)
from bot.exchange.client import BybitClient
from bot.models import ClosedTrade, Position, Side, Signal
from bot.risk.limits import RiskConfig
from bot.strategy.base import Management
from bot.strategy.trend import TrendFollowing, atr, ema
from tests.fakes import FakeSession

START = datetime(2022, 1, 1, tzinfo=UTC)


def synthetic(
    n: int = 1500, seed: int = 1, start_price: float = 30_000, drift: float = 0.0
) -> pd.DataFrame:
    """Marché synthétique : régimes de tendance alternés + bruit."""
    rng = np.random.default_rng(seed)
    regime = np.sign(np.sin(np.arange(n) / 120)) * 0.002 + drift
    rets = regime + rng.normal(0, 0.01, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    idx = pd.date_range(START, periods=n, freq=BAR, tz=UTC, name="time")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx
    )


def kline_rows(df: pd.DataFrame) -> list[list[str]]:
    return [
        [str(int(t.timestamp() * 1000)), *(str(v) for v in row[:5]), "0"]
        for t, row in zip(df.index, df.to_numpy(), strict=True)
    ]


# ---------------------------------------------------------------- données
def test_rows_to_frame_and_download() -> None:
    df = synthetic(2500)
    s = FakeSession()
    s.klines["BTCUSDT"] = kline_rows(df)
    out = download_klines(BybitClient(s), "BTCUSDT", START, START + 2499 * BAR)
    assert len(out) == 2500
    assert out.index.is_monotonic_increasing
    assert rows_to_frame([]).empty
    assert is_valid(out) and not find_gaps(out)


def test_funding_download_paginates_backwards() -> None:
    times = [START + timedelta(hours=8 * i) for i in range(450)]

    class Src:
        def funding_history(
            self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
        ) -> list[dict[str, str]]:
            assert start_ms is not None and end_ms is not None
            sel = [t for t in times if start_ms <= t.timestamp() * 1000 <= end_ms]
            return [
                {"fundingRateTimestamp": str(int(t.timestamp() * 1000)), "fundingRate": "0.0001"}
                for t in sel[-200:]
            ]

    s = download_funding(Src(), "BTCUSDT", START, times[-1])  # type: ignore[arg-type]
    assert len(s) == 450 and s.iloc[0] == 0.0001


def test_quality_checks() -> None:
    df = synthetic(10)
    assert find_gaps(df.drop(df.index[4])) == [df.index[4]]
    assert find_gaps(df.iloc[:1]) == []
    bad = df.copy()
    bad.loc[bad.index[2], "high"] = 0
    assert not is_valid(bad)
    assert not is_valid(df.iloc[:0])
    now = df.index[-1].to_pydatetime() + timedelta(hours=1)
    assert len(closed_bars(df, now)) == 9  # bougie en cours retirée
    assert len(closed_bars(df, now + BAR)) == 10
    assert closed_bars(df.iloc[:0], now).empty


def test_csv_roundtrip(tmp_path: Path) -> None:
    df = synthetic(20)
    save_csv(df, tmp_path / "x.csv")
    pd.testing.assert_frame_equal(load_candles(tmp_path / "x.csv"), df, check_freq=False)
    rates = pd.Series([0.0001], index=pd.DatetimeIndex([START], name="time"), name="rate")
    save_csv(rates, tmp_path / "f.csv")
    assert load_funding(tmp_path / "f.csv").iloc[0] == 0.0001
    assert load_funding(tmp_path / "missing.csv").empty


# ---------------------------------------------------------------- stratégie
def test_indicators() -> None:
    s = pd.Series([1.0] * 10)
    assert ema(s, 3).iloc[-1] == 1
    df = synthetic(50)
    assert (atr(df) > 0).all()


def _uptrend(n: int = 300) -> pd.DataFrame:
    idx = pd.date_range(START, periods=n, freq=BAR, tz=UTC, name="time")
    close = np.linspace(100, 200, n)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )


def test_trend_long_signal_and_warmup() -> None:
    strat = TrendFollowing()
    bars = _uptrend()
    bars.loc[bars.index[-1], "close"] = 210  # cassure du plus haut 20 bougies
    bars.loc[bars.index[-1], "high"] = 211
    sig = strat.signal("BTCUSDT", bars)
    assert sig is not None and sig.side is Side.LONG and sig.stop < sig.entry
    assert strat.signal("BTCUSDT", bars.iloc[:100]) is None


def test_trend_short_signal() -> None:
    bars = _uptrend().iloc[::-1].copy()
    bars.index = pd.date_range(START, periods=len(bars), freq=BAR, tz=UTC, name="time")
    bars.loc[bars.index[-1], "close"] = 90
    bars.loc[bars.index[-1], "low"] = 89
    sig = TrendFollowing().signal("BTCUSDT", bars)
    assert sig is not None and sig.side is Side.SHORT and sig.stop > sig.entry


def test_no_signal_inside_range() -> None:
    bars = _uptrend()
    bars.loc[bars.index[-1], "close"] = 150
    assert TrendFollowing().signal("BTCUSDT", bars) is None


def test_manage_breakeven_trailing_and_exit() -> None:
    strat = TrendFollowing()
    bars = _uptrend()
    opened = bars.index[-30].to_pydatetime()
    entry = float(bars["close"].iloc[-30])
    p = Position("BTCUSDT", Side.LONG, 1, entry, entry - 5, entry - 5, opened)
    m = strat.manage(p, bars)
    assert m.new_stop is not None and m.new_stop >= entry
    far = Position("BTCUSDT", Side.LONG, 1, entry, entry - 500, entry - 500, opened)
    assert strat.manage(far, bars) == Management()  # pas encore +1R
    dump = bars.copy()
    dump.loc[dump.index[-1], "close"] = 100
    assert strat.manage(p, dump).exit
    short = Position("BTCUSDT", Side.SHORT, 1, entry, entry + 5, entry + 5, opened)
    pump = bars.copy()
    pump.loc[pump.index[-1], "close"] = 250
    assert strat.manage(short, pump).exit  # clôture au-dessus du plus haut 10 bougies
    assert strat.manage(p, bars.iloc[:5]) == Management()


def test_manage_short_trailing() -> None:
    bars = _uptrend().iloc[::-1].copy()
    bars.index = pd.date_range(START, periods=len(bars), freq=BAR, tz=UTC, name="time")
    opened = bars.index[-30].to_pydatetime()
    entry = float(bars["close"].iloc[-30])
    p = Position("BTCUSDT", Side.SHORT, 1, entry, entry + 5, entry + 5, opened)
    m = TrendFollowing().manage(p, bars)
    assert m.new_stop is not None and m.new_stop <= entry


# ---------------------------------------------------------------- moteur
def test_backtest_runs_with_costs() -> None:
    bars = {"BTCUSDT": synthetic(1500, 1), "ETHUSDT": synthetic(1500, 2, 2000)}
    funding = {s: pd.Series(0.0001, index=df.index[::2]) for s, df in bars.items()}
    res = run_backtest(bars, TrendFollowing(), RiskConfig(), funding=funding)
    assert res.trades, "la stratégie doit trader sur un marché à tendances"
    assert all(t.fees > 0 for t in res.trades)
    assert any(t.funding != 0 for t in res.trades)
    m = compute_metrics(res)
    assert m.trades == len(res.trades)
    # Le dernier point d'equity = capital + somme des P&L nets.
    assert res.equity.iloc[-1] == pytest.approx(1000 + sum(t.net_pnl for t in res.trades))
    for t in res.trades:
        assert t.risk_usdt <= 1000 * 0.02 * 1.5  # risque contenu malgré la croissance de l'equity


def test_backtest_no_lookahead() -> None:
    """Modifier le futur ne change aucun trade passé."""
    df = synthetic(1200, 3)
    cut = df.index[900]
    altered = df.copy()
    altered.loc[altered.index > cut, ["open", "high", "low", "close"]] *= 1.5
    a = run_backtest({"BTCUSDT": df}, TrendFollowing(), RiskConfig())
    b = run_backtest({"BTCUSDT": altered}, TrendFollowing(), RiskConfig())
    done_a = [t for t in a.trades if t.closed_at <= cut]
    done_b = [t for t in b.trades if t.closed_at <= cut]
    assert done_a == done_b and done_a


def test_gap_through_stop_and_stop_hit() -> None:
    idx = pd.date_range(START, periods=4, freq=BAR, tz=UTC, name="time")
    df = pd.DataFrame(
        {
            "open": [100, 100, 80, 100],
            "high": [101, 101, 81, 101],
            "low": [99, 99, 79, 99],
            "close": [100, 100, 80, 100],
            "volume": 1.0,
        },
        index=idx,
    )

    class Once:
        name = "once"
        warmup = 1

        def __init__(self, stop: float) -> None:
            self.stop = stop
            self.fired = False

        def signal(self, symbol: str, bars: pd.DataFrame) -> Signal | None:
            if self.fired:
                return None
            self.fired = True
            return Signal(symbol, Side.LONG, 100, self.stop, bars.index[-1].to_pydatetime(), "once")

        def manage(self, position: Position, bars: pd.DataFrame) -> Management:
            return Management()

    # Stop à 95 ; la bougie 2 ouvre à 100, la 3 gappe à 80 → sortie à l'ouverture.
    res = run_backtest({"BTCUSDT": df}, Once(95), RiskConfig(), initial_equity=100_000)
    assert len(res.trades) == 1
    assert res.trades[0].exit == pytest.approx(80 * (1 - 0.0005))
    assert res.trades[0].r_multiple < -1  # le gap coûte plus de 1R : c'est réaliste

    # Ouverture suivante déjà sous le stop : pas d'entrée.
    df2 = df.copy()
    df2.iloc[1] = [90, 91, 89, 90, 1]
    res2 = run_backtest({"BTCUSDT": df2}, Once(95), RiskConfig(), initial_equity=100_000)
    assert res2.trades == []


def test_strategy_exit_and_trailing_in_engine() -> None:
    idx = pd.date_range(START, periods=6, freq=BAR, tz=UTC, name="time")
    df = pd.DataFrame(
        {"open": [100] * 6, "high": [101] * 6, "low": [99] * 6, "close": [100] * 6, "volume": 1.0},
        index=idx,
    )

    class Scripted:
        name = "s"
        warmup = 1
        fired = False

        def signal(self, symbol: str, bars: pd.DataFrame) -> Signal | None:
            if self.fired:
                return None
            self.fired = True
            return Signal(symbol, Side.LONG, 100, 95, bars.index[-1].to_pydatetime(), "s")

        def manage(self, position: Position, bars: pd.DataFrame) -> Management:
            if position.stop < 97:
                return Management(new_stop=97)
            return Management(exit=True)

    res = run_backtest({"BTCUSDT": df}, Scripted(), RiskConfig(), initial_equity=100_000)
    assert len(res.trades) == 1 and res.trades[0].exit_reason == "sortie stratégie"


# ---------------------------------------------------------------- métriques
def _trade(pnl: float) -> ClosedTrade:
    return ClosedTrade("BTCUSDT", Side.LONG, 1, 100, 100 + pnl, 90, START, START, 0, 0, "x")


def test_metric_helpers() -> None:
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    assert profit_factor([_trade(10), _trade(-5)]) == 2
    assert profit_factor([_trade(10)]) == math.inf
    assert profit_factor([]) == 0
    assert monte_carlo_drawdown([]) == {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    mc = monte_carlo_drawdown([0.01, -0.01, 0.02, -0.02] * 10, runs=200)
    assert 0 < mc["p50"] <= mc["p95"] <= mc["p99"] < 1


def test_walk_forward_and_go_no_go() -> None:
    bars = {"BTCUSDT": synthetic(2000, 5)}
    wf = walk_forward(bars, TrendFollowing(), RiskConfig())
    assert wf.oos_result.equity.index[0] >= wf.split
    assert all(t.opened_at >= wf.split for t in wf.oos_result.trades)
    full = compute_metrics(run_backtest(bars, TrendFollowing(), RiskConfig()))
    crit = go_no_go(full, wf.out_of_sample)
    assert len(crit) == 5
    assert not crit[1].passed  # < 100 trades sur données synthétiques courtes


def test_empty_result_metrics() -> None:
    res = run_backtest({"BTCUSDT": synthetic(50)}, TrendFollowing(), RiskConfig())
    m = compute_metrics(res)
    assert m.trades == 0 and m.win_rate == 0 and m.avg_r == 0


def test_report_and_cli(tmp_path: Path) -> None:
    s = FakeSession()
    end = datetime.now(UTC)
    for sym, seed in (("BTCUSDT", 1), ("ETHUSDT", 2)):
        df = synthetic(1500, seed)
        df.index = pd.date_range(end - 1500 * BAR, periods=1500, freq=BAR, tz=UTC, name="time")
        s.klines[sym] = kline_rows(df)
    download(0.7, ["BTCUSDT", "ETHUSDT"], client=BybitClient(s), data_dir=tmp_path)
    out = tmp_path / "r.html"
    passed = run(["BTCUSDT", "ETHUSDT"], out, 1000, 0.0005, tmp_path)
    html = out.read_text()
    assert "Verdict go/no-go" in html and "Walk-forward" in html
    assert passed is False  # trop court pour 3 ans
    assert main(["run", "--data-dir", str(tmp_path), "--out", str(out)]) == 1


def test_report_with_tiny_series() -> None:
    res = run_backtest({"BTCUSDT": synthetic(1)}, TrendFollowing(), RiskConfig())
    full = compute_metrics(res)
    wf = walk_forward({"BTCUSDT": synthetic(300)}, TrendFollowing(), RiskConfig())
    html = render_report(res, full, wf, monte_carlo_drawdown([]), go_no_go(full, wf.out_of_sample))
    assert "Pas assez de points" in html
