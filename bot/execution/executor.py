"""Exécution : ordres, stops, clôtures, kill switch.

Seul le module de risque produit les décisions passées à ``open`` : la
stratégie n'a aucun accès direct à cette classe.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from bot.alerts import Notifier
from bot.exchange.client import BybitClient, ExchangeError, ExchangePosition
from bot.exchange.instruments import InstrumentRules
from bot.models import ClosedTrade, Position, Side
from bot.risk.limits import STOP_CONFIRM_TIMEOUT_S
from bot.risk.manager import Decision, RiskManager, tighten_stop

log = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        client: BybitClient,
        risk: RiskManager,
        rules: dict[str, InstrumentRules],
        notifier: Notifier,
        *,
        fee_taker: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.risk = risk
        self.rules = rules
        self.notifier = notifier
        self.fee_taker = fee_taker
        self._sleep = sleep
        self._clock = clock

    def _find(self, symbol: str) -> ExchangePosition | None:
        return next((p for p in self.client.positions() if p.symbol == symbol), None)

    # ------------------------------------------------------------------ entrée
    def open(self, decision: Decision) -> Position | None:
        """Entrée au marché avec stop attaché, puis vérification du stop chez Bybit sous 5 s."""
        if not decision.approved:
            raise ValueError("Décision non approuvée par le module de risque")
        sig = decision.signal
        rules = self.rules[sig.symbol]
        qty = rules.floor_qty(decision.qty)
        stop = Decimal(str(decision.stop))
        self.client.open_with_stop(sig.symbol, sig.side, qty, stop, sig.link_id)

        found: ExchangePosition | None = None
        waited = 0.0
        while waited <= STOP_CONFIRM_TIMEOUT_S:
            found = self._find(sig.symbol)
            if found is not None and found.stop is not None:
                break
            self._sleep(0.5)
            waited += 0.5

        if found is None:
            self.notifier.send(
                f"Ordre {sig.symbol} envoyé mais aucune position visible après 5 s : vérification requise",
                level="ERROR",
                kind="execution",
            )
            return None
        if found.stop is None:
            self.client.close_market(found.symbol, found.side, found.qty)
            self.notifier.send(
                f"{sig.symbol} rempli SANS stop visible après 5 s : clôture immédiate au marché",
                level="CRITICAL",
                kind="execution",
            )
            return None

        position = Position(
            symbol=found.symbol,
            side=found.side,
            qty=found.qty,  # quantité réellement remplie (remplissage partiel compris)
            entry=found.entry,
            stop=found.stop,
            initial_stop=found.stop,
            opened_at=self._clock(),
            strategy=sig.strategy,
            liq_price=found.liq_price,
            mark_price=found.mark_price,
            fees=found.qty * found.entry * self.fee_taker,
        )
        if found.qty < float(qty):
            self.notifier.send(
                f"{sig.symbol} remplissage partiel {found.qty}/{qty} : stop sur toute la position",
                level="WARNING",
                kind="execution",
            )
        self.notifier.send(
            f"Entrée {position.side.value} {position.symbol} {position.qty} @ {position.entry:,.2f}, "
            f"stop {position.stop:,.2f}, risque {decision.risk_usdt:,.2f} USDT",
            kind="entry",
        )
        return position

    # ------------------------------------------------------------------ gestion
    def move_stop(self, position: Position, proposed: float, price: float) -> Position:
        """Rapproche le stop (break-even, trailing). Ne l'éloigne jamais."""
        candidate = tighten_stop(position, proposed, price)
        if candidate is None:
            return position
        rules = self.rules[position.symbol]
        # Arrondi vers le prix d'entrée… sauf s'il franchit le prix courant.
        rounded = float(rules.round_stop(candidate, long=position.side is Side.LONG))
        final = tighten_stop(position, rounded, price)
        if final is None:
            final = float(rules.round_stop(candidate, long=position.side is not Side.LONG))
            if tighten_stop(position, final, price) is None:
                return position
        self.client.set_stop(position.symbol, Decimal(str(final)))
        return replace(position, stop=final)

    def close(self, position: Position, reason: str) -> None:
        self.client.close_market(position.symbol, position.side, position.qty)
        self.notifier.send(f"Sortie demandée {position.symbol} : {reason}", kind="exit")

    def closed_trade(self, position: Position, reason: str) -> ClosedTrade:
        """Construit le trade clos à partir de ``closed-pnl`` de Bybit (qui fait foi)."""
        exit_price = position.mark_price or position.entry
        net: float | None = None
        opened_ms = int(position.opened_at.timestamp() * 1000)
        try:
            rows = [
                r
                for r in self.client.closed_pnl(position.symbol, limit=5)
                if int(r.get("updatedTime") or r.get("createdTime") or 0) >= opened_ms
            ]
        except ExchangeError:
            rows = []
        if not rows:
            with contextlib.suppress(ExchangeError):
                exit_price = self.client.mark_price(position.symbol)
        if rows:
            row = rows[0]
            exit_price = float(row.get("avgExitPrice") or exit_price)
            net = float(row["closedPnl"]) if row.get("closedPnl") not in (None, "") else None
        gross = (exit_price - position.entry) * position.side.sign * position.qty
        fees = position.fees + position.qty * exit_price * self.fee_taker
        # Résidu entre P&L brut, frais et P&L net Bybit : funding et ajustements.
        funding = gross - fees - net if net is not None else position.funding
        return ClosedTrade(
            position.symbol,
            position.side,
            position.qty,
            position.entry,
            exit_price,
            position.initial_stop,
            position.opened_at,
            self._clock(),
            fees,
            funding,
            reason,
            position.strategy,
        )

    # ------------------------------------------------------------------ kill switch
    def kill(self, reason: str) -> None:
        """Annule tous les ordres, ferme toutes les positions au marché, passe en HALT."""
        self.risk.kill(reason)
        errors: list[str] = []
        try:
            self.client.cancel_all()
        except ExchangeError as exc:
            errors.append(f"annulation : {exc}")
        try:
            for p in self.client.positions():
                try:
                    self.client.close_market(p.symbol, p.side, p.qty)
                except ExchangeError as exc:
                    errors.append(f"clôture {p.symbol} : {exc}")
        except ExchangeError as exc:
            errors.append(f"lecture des positions : {exc}")
        level = "CRITICAL" if errors else "WARNING"
        detail = f" — ERREURS : {'; '.join(errors)}" if errors else ""
        self.notifier.send(f"KILL SWITCH ({reason}) : ordres annulés, positions fermées, mode HALT{detail}",
                           level=level, kind="kill")  # fmt: skip
