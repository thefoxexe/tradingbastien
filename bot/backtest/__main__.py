"""CLI du backtest.

python -m bot.backtest download --years 3      # bougies 4 h + funding (endpoints publics)
python -m bot.backtest run                      # rapport HTML dans reports/backtest.html
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.backtest.engine import DEFAULT_SLIPPAGE, run_backtest
from bot.backtest.metrics import compute_metrics, go_no_go, monte_carlo_drawdown, walk_forward
from bot.backtest.report import render_report, write_report
from bot.config import ALLOWED_SYMBOLS
from bot.data.candles import download_funding, download_klines, load_candles, load_funding, save_csv
from bot.exchange.client import BybitClient, Session
from bot.risk.limits import RiskConfig
from bot.strategy.trend import TrendFollowing

DATA_DIR = Path("data")


def _public_client() -> BybitClient:  # pragma: no cover - réseau
    from pybit.unified_trading import HTTP  # noqa: PLC0415

    session: Session = HTTP(testnet=False)
    return BybitClient(session)


def download(
    years: float, symbols: list[str], client: BybitClient | None = None, data_dir: Path = DATA_DIR
) -> None:
    client = client or _public_client()
    end = datetime.now(UTC)
    start = end - timedelta(days=int(365.25 * years) + 60)  # + marge de chauffe des indicateurs
    for sym in symbols:
        candles = download_klines(client, sym, start, end)
        save_csv(candles, data_dir / f"{sym}_240.csv")
        rates = download_funding(client, sym, start, end)
        save_csv(rates, data_dir / f"{sym}_funding.csv")
        print(f"{sym}: {len(candles)} bougies, {len(rates)} taux de funding")


def run(
    symbols: list[str], out: Path, equity: float, slippage: float, data_dir: Path = DATA_DIR
) -> bool:
    bars = {s: load_candles(data_dir / f"{s}_240.csv") for s in symbols}
    funding = {s: load_funding(data_dir / f"{s}_funding.csv") for s in symbols}
    strategy = TrendFollowing()
    cfg = RiskConfig()
    result = run_backtest(
        bars, strategy, cfg, initial_equity=equity, funding=funding, slippage=slippage
    )
    full = compute_metrics(result)
    wf = walk_forward(
        bars, strategy, cfg, initial_equity=equity, funding=funding, slippage=slippage
    )
    mc = monte_carlo_drawdown(result.trade_returns)
    criteria = go_no_go(full, wf.out_of_sample)
    notes = (
        f"{', '.join(symbols)} · capital initial {equity:,.0f} USDT · risque {cfg.risk_per_trade:.1%}/trade · "
        f"frais taker {cfg.fee_taker:.3%} · slippage {slippage:.2%}/côté · funding historique"
    )
    write_report(render_report(result, full, wf, mc, criteria, notes=notes), out)
    passed = all(c.passed for c in criteria)
    print(f"Rapport : {out}  —  {'PASSE' if passed else 'ÉCHEC'}")
    for c in criteria:
        print(f"  [{'OK' if c.passed else 'NON'}] {c.name}: {c.value}")
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.backtest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--years", type=float, default=3.0)
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, default=Path("reports/backtest.html"))
    r.add_argument("--equity", type=float, default=1000.0)
    r.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE)
    for p in (d, r):
        p.add_argument(
            "--symbols", nargs="+", default=sorted(ALLOWED_SYMBOLS), choices=sorted(ALLOWED_SYMBOLS)
        )
        p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)
    if args.cmd == "download":
        download(args.years, args.symbols, data_dir=args.data_dir)
        return 0
    return 0 if run(args.symbols, args.out, args.equity, args.slippage, args.data_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
