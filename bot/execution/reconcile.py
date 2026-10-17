"""Réconciliation : Bybit fait toujours foi.

Au démarrage et toutes les 60 s, l'état local est comparé à l'exchange.
Tant qu'un écart n'est pas résolu, aucune nouvelle entrée n'est autorisée.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from bot.exchange.client import ExchangePosition
from bot.models import Position, Side

# Ordres conditionnels créés par Bybit pour porter le stop d'une position.
_TPSL_TYPES = frozenset(
    {"StopLoss", "TakeProfit", "TrailingStop", "PartialStopLoss", "PartialTakeProfit"}
)


@dataclass
class ReconcileReport:
    positions: dict[str, Position]
    closed: list[Position] = field(default_factory=list)  # disparues chez Bybit (stop touché, etc.)
    to_close: list[ExchangePosition] = field(default_factory=list)  # sans stop : à fermer
    stop_fixes: list[tuple[str, float]] = field(
        default_factory=list
    )  # stops locaux plus serrés à reposer
    issues: list[str] = field(default_factory=list)  # écarts non résolus : bloquent les entrées
    notes: list[str] = field(default_factory=list)  # informations, n'empêchent pas de trader

    @property
    def healthy(self) -> bool:
        return not self.issues and not self.to_close


def _tighter(side: Side, a: float, b: float) -> float:
    return max(a, b) if side is Side.LONG else min(a, b)


def reconcile(
    local: dict[str, Position],
    exchange: list[ExchangePosition],
    open_orders: list[dict[str, Any]],
    now: datetime,
) -> ReconcileReport:
    out: dict[str, Position] = {}
    report = ReconcileReport(positions=out)
    remote = {p.symbol: p for p in exchange}

    for sym, ex in remote.items():
        if ex.stop is None:
            report.to_close.append(ex)
            report.issues.append(f"{sym} : position sans stop chez Bybit, clôture au marché")
            continue
        mine = local.get(sym)
        if mine is None:
            # Redémarrage sans base, ou position ouverte hors bot : reconstruite depuis Bybit.
            out[sym] = Position(
                sym, ex.side, ex.qty, ex.entry, ex.stop, ex.stop, ex.created_at or now,
                liq_price=ex.liq_price, mark_price=ex.mark_price, unrealized_pnl=ex.unrealized_pnl,
            )  # fmt: skip
            report.notes.append(
                f"{sym} : position reconstruite depuis Bybit (stop initial inconnu, pris = stop actuel)"
            )
            continue
        if mine.side is not ex.side:
            report.issues.append(f"{sym} : sens local {mine.side.value} ≠ Bybit {ex.side.value}")
            out[sym] = replace(
                mine, side=ex.side, qty=ex.qty, entry=ex.entry, stop=ex.stop, initial_stop=ex.stop
            )
            continue
        if abs(mine.qty - ex.qty) > 1e-12:
            report.notes.append(
                f"{sym} : quantité ajustée {mine.qty} → {ex.qty} (exécutions réelles)"
            )
        stop = ex.stop
        if _tighter(ex.side, mine.stop, ex.stop) != ex.stop:
            # Le stop Bybit est plus large que le nôtre : on repose le nôtre (jamais l'inverse).
            report.stop_fixes.append((sym, mine.stop))
            stop = mine.stop
        out[sym] = replace(
            mine,
            qty=ex.qty,
            entry=ex.entry,
            stop=stop,
            liq_price=ex.liq_price,
            mark_price=ex.mark_price,
            unrealized_pnl=ex.unrealized_pnl,
        )

    for sym, mine in local.items():
        if sym not in remote:
            report.closed.append(mine)

    for o in open_orders:
        if o.get("stopOrderType") in _TPSL_TYPES or str(o.get("reduceOnly")).lower() == "true":
            continue
        report.issues.append(
            f"ordre ouvert inattendu : {o.get('symbol')} {o.get('side')} {o.get('orderLinkId')}"
        )

    return report
