"""Boucle principale : données → stratégie → risque → exécution, plus la réconciliation.

Chaque ``tick`` :
1. exécute les commandes du dashboard / Telegram (pause, kill) ;
2. réconcilie avec Bybit toutes les 60 s (et au démarrage) ;
3. à chaque nouvelle bougie 4 h close : gère les positions, puis cherche des entrées.

En cas de doute (erreur API, écart, données manquantes), aucune entrée.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from bot.alerts import Notifier
from bot.data.candles import BAR, INTERVAL, closed_bars, find_gaps, is_valid, rows_to_frame
from bot.exchange.client import BybitClient, ExchangeError
from bot.exchange.instruments import InstrumentRules
from bot.exchange.stream import MarketStream
from bot.execution.executor import Executor
from bot.execution.reconcile import reconcile
from bot.models import BotStatus, Position
from bot.risk.manager import RiskManager, RiskState
from bot.store import Store
from bot.strategy.base import Strategy

log = logging.getLogger(__name__)

HISTORY_BARS = 1000
MAX_ENTRY_DELAY = timedelta(minutes=15)  # au-delà, l'entrée « juste après la clôture » est périmée


class Bot:
    def __init__(
        self,
        *,
        client: BybitClient,
        store: Store,
        notifier: Notifier,
        risk: RiskManager,
        executor: Executor,
        strategy: Strategy,
        rules: dict[str, InstrumentRules],
        symbols: list[str],
        mode: str,
        reconcile_every: timedelta = timedelta(seconds=60),
        stream: MarketStream | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.notifier = notifier
        self.risk = risk
        self.executor = executor
        self.strategy = strategy
        self.rules = rules
        self.symbols = symbols
        self.mode = mode
        self.reconcile_every = reconcile_every
        self.stream = stream
        self.heartbeat = heartbeat
        self.positions: dict[str, Position] = store.load_positions()
        self.last_bar: dict[str, datetime] = {
            s: datetime.fromisoformat(v) for s, v in (store.get("last_bar") or {}).items()
        }
        self.last_reconcile: datetime | None = None
        self.healthy = False
        self.equity = 0.0
        self.last_summary_day = store.get("last_summary_day")
        saved = store.get("risk_state")
        if saved:
            risk.state = RiskState.from_dict(saved)

    # ------------------------------------------------------------------ utilitaires
    def _alert(self, text: str, level: str = "WARNING", kind: str = "info") -> None:
        self.notifier.send(text, level=level, kind=kind)

    def _flush_risk_events(self) -> None:
        for e in self.risk.events:
            self._alert(f"Limite / statut : {e}", level="WARNING", kind="risk")
        self.risk.events.clear()

    def persist(self, now: datetime) -> None:
        self.store.put("risk_state", self.risk.state.to_dict())
        self.store.save_positions(self.positions)
        self.store.put("last_bar", {s: t.isoformat() for s, t in self.last_bar.items()})
        self.store.put(
            "snapshot",
            {
                "ts": now.isoformat(),
                "mode": self.mode,
                "status": self.risk.state.status.value,
                "status_reason": self.risk.state.status_reason,
                "pause_until": self.risk.state.pause_until.isoformat()
                if self.risk.state.pause_until
                else None,
                "equity": self.equity,
                "healthy": self.healthy,
                "daily_pnl_pct": self.risk.daily_pnl_pct(),
                "weekly_pnl_pct": self.risk.weekly_pnl_pct(),
                "drawdown": self.risk.drawdown(),
                "peak_equity": self.risk.state.peak_equity,
                "consecutive_losses": self.risk.state.consecutive_losses,
                "open_risk": sum(p.open_risk for p in self.positions.values()),
                "limits": self.risk.cfg.as_table(),
            },
        )

    def _record_close(self, pos: Position, reason: str, now: datetime) -> None:
        trade = self.executor.closed_trade(pos, reason)
        self.store.add_trade(trade)
        self.risk.record_trade(trade.net_pnl, now)
        self.positions.pop(pos.symbol, None)
        self._alert(
            f"Sortie {trade.symbol} @ {trade.exit:,.2f} ({reason}) : {trade.net_pnl:+,.2f} USDT ({trade.r_multiple:+.2f} R)",
            level="INFO",
            kind="exit",
        )

    # ------------------------------------------------------------------ commandes
    def process_commands(self) -> None:
        for command, source in self.store.pop_commands():
            if command == "kill":
                self.executor.kill(f"kill switch ({source})")
                for pos in list(self.positions.values()):
                    self._record_close(pos, "kill switch", datetime.now(UTC))
            elif command == "pause":
                self.risk.pause(f"pause manuelle ({source})")
                self._alert(
                    f"Pause demandée depuis {source} : plus d'entrée, stops en place", kind="pause"
                )

    # ------------------------------------------------------------------ réconciliation
    def do_reconcile(self, now: datetime) -> None:
        try:
            self.equity = self.client.equity()
            remote = self.client.positions()
            orders = self.client.open_orders()
        except ExchangeError as exc:
            self.healthy = False
            self._alert(f"Erreur API pendant la réconciliation : {exc}", level="ERROR", kind="api")
            return
        report = reconcile(self.positions, remote, orders, now)
        for ex in report.to_close:
            try:
                self.client.close_market(ex.symbol, ex.side, ex.qty)
            except ExchangeError as exc:
                report.issues.append(f"échec de clôture {ex.symbol} : {exc}")
        for sym, stop in report.stop_fixes:
            try:
                self.executor.client.set_stop(
                    sym, self.rules[sym].round_stop(stop, long=report.positions[sym].side.sign > 0)
                )
            except ExchangeError as exc:
                report.issues.append(f"échec de repose du stop {sym} : {exc}")
        for gone in report.closed:
            self.positions[gone.symbol] = gone
            self._record_close(gone, "stop touché ou clôture chez Bybit", now)
        for note in report.notes:
            self.store.log_event("reconcile", note)
        if report.issues:
            self._alert(
                "Écart de réconciliation : " + " ; ".join(report.issues),
                level="ERROR",
                kind="reconcile",
            )
        self.positions = report.positions
        self.healthy = report.healthy
        self.risk.update_equity(self.equity, now)
        self.store.record_equity(now.replace(second=0, microsecond=0), self.equity)
        self.last_reconcile = now

    # ------------------------------------------------------------------ bougies
    def _bars(self, symbol: str, now: datetime) -> Any:
        rows = self.client.klines(symbol, INTERVAL, limit=HISTORY_BARS)
        df = closed_bars(rows_to_frame(rows), now)
        if df.empty or not is_valid(df) or find_gaps(df):
            return None
        return df

    def on_bars(self, now: datetime) -> None:
        for sym in self.symbols:
            expected = (
                datetime.fromtimestamp(
                    (now.timestamp() // BAR.total_seconds()) * BAR.total_seconds(), UTC
                )
                - BAR
            )
            if self.last_bar.get(sym) == expected:
                continue
            try:
                bars = self._bars(sym, now)
            except ExchangeError as exc:
                self._alert(
                    f"{sym} : bougies indisponibles ({exc}) — signal ignoré",
                    level="ERROR",
                    kind="data",
                )
                continue
            if bars is None or bars.index[-1].to_pydatetime() != expected:
                self._alert(
                    f"{sym} : bougie incomplète ou trou de données — signal ignoré",
                    level="WARNING",
                    kind="data",
                )
                self.last_bar[sym] = expected
                continue
            self.last_bar[sym] = expected
            fresh = now - (expected + BAR) <= MAX_ENTRY_DELAY
            self._handle_symbol(sym, bars, now, allow_entry=fresh)

    def _handle_symbol(
        self, sym: str, bars: Any, now: datetime, *, allow_entry: bool = True
    ) -> None:
        last_close = float(bars["close"].iloc[-1])
        pos = self.positions.get(sym)
        if pos is not None:
            m = self.strategy.manage(pos, bars)
            try:
                if m.exit:
                    self.executor.close(pos, m.reason)
                    self._record_close(pos, m.reason, now)
                elif m.new_stop is not None:
                    self.positions[sym] = self.executor.move_stop(pos, m.new_stop, last_close)
            except ExchangeError as exc:
                self.healthy = False
                self._alert(
                    f"{sym} : gestion de position impossible ({exc})", level="ERROR", kind="api"
                )
            return

        sig = self.strategy.signal(sym, bars)
        if sig is None:
            return
        if not allow_entry:
            self.store.log_event(
                "veto", f"{sym} {sig.side.value} ignoré : bougie close depuis plus de 15 min"
            )
            return
        try:
            mark = self.client.mark_price(sym)
        except ExchangeError as exc:
            self._alert(
                f"{sym} : prix indisponible ({exc}) — signal ignoré", level="ERROR", kind="api"
            )
            return
        sig = replace(sig, entry=mark)  # entrée au marché juste après la clôture
        stale = self.stream is not None and self.stream.is_stale()
        decision = self.risk.evaluate(
            sig,
            equity=self.equity,
            positions=self.positions.values(),
            rules=self.rules[sym],
            now=now,
            healthy=self.healthy and not stale,
        )
        if not decision.approved:
            self.store.log_event("veto", f"{sym} {sig.side.value} refusé : {decision.reason}")
            return
        try:
            position = self.executor.open(decision)
        except ExchangeError as exc:
            self.healthy = False
            self._alert(f"{sym} : ordre refusé par Bybit ({exc})", level="ERROR", kind="api")
            return
        if position is not None:
            self.positions[sym] = position
        else:
            self.healthy = False

    # ------------------------------------------------------------------ résumé
    def daily_summary(self, now: datetime) -> None:
        day = now.date().isoformat()
        if self.last_summary_day == day:
            return
        if self.last_summary_day is not None:
            since = now - timedelta(days=1)
            todays = [t for t in self.store.list_trades(200) if t["closed_at"] >= since]
            pnl = sum(t["net_pnl"] for t in todays)
            self._alert(
                f"Résumé quotidien : equity {self.equity:,.2f} USDT, P&L 24 h {pnl:+,.2f} USDT, "
                f"{len(todays)} trade(s), statut {self.risk.state.status.value}",
                level="INFO",
                kind="summary",
            )
        self.last_summary_day = day
        self.store.put("last_summary_day", day)

    # ------------------------------------------------------------------ tick
    def tick(self, now: datetime) -> None:
        self.process_commands()
        if self.last_reconcile is None or now - self.last_reconcile >= self.reconcile_every:
            self.do_reconcile(now)
        if self.risk.state.status is not BotStatus.HALT:
            self.on_bars(now)
        self.daily_summary(now)
        self._flush_risk_events()
        self.persist(now)
        if self.heartbeat is not None:
            self.heartbeat()

    def run_forever(
        self, interval_s: float, stop: Callable[[], bool] = lambda: False
    ) -> None:  # pragma: no cover
        while not stop():
            started = time.monotonic()
            try:
                self.tick(datetime.now(UTC))
            except Exception as exc:
                self.healthy = False
                log.exception("erreur dans la boucle")
                self._alert(
                    f"Erreur inattendue dans la boucle : {type(exc).__name__}: {exc}",
                    level="ERROR",
                    kind="error",
                )
            time.sleep(max(0.0, interval_s - (time.monotonic() - started)))


def http_heartbeat(url: str) -> Callable[[], None]:
    """Ping externe (healthchecks.io, Uptime Kuma…) au plus une fois par minute."""
    last = [0.0]

    def ping() -> None:
        if time.monotonic() - last[0] < 60:
            return
        last[0] = time.monotonic()
        try:
            httpx.get(url, timeout=5)
        except httpx.HTTPError:
            log.warning("heartbeat injoignable")

    return ping
