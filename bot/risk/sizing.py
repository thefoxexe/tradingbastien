"""Taille de position : elle découle de la distance au stop, jamais l'inverse.

    qty = equity × r / (|entry − stop| + entry × 2 × fee_taker)

Puis levier effectif = qty × entry / equity ; s'il dépasse le plafond, la
quantité est réduite, jamais augmentée. Arrondi vers le bas au ``qtyStep``.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.exchange.instruments import InstrumentRules
from bot.risk.limits import RiskConfig


@dataclass(frozen=True)
class Size:
    qty: float
    effective_leverage: float
    risk_usdt: float  # perte au stop, frais aller-retour compris


def liquidation_price(entry: float, *, long: bool, leverage: float, mmr: float) -> float:
    """Approximation du prix de liquidation en marge isolée."""
    if long:
        return entry * (1 - 1 / leverage + mmr)
    return entry * (1 + 1 / leverage - mmr)


def compute_size(
    equity: float, entry: float, stop: float, cfg: RiskConfig, rules: InstrumentRules
) -> Size | str:
    """Renvoie la taille, ou la raison du refus (chaîne)."""
    if equity <= 0 or entry <= 0 or stop <= 0:
        return "equity, entrée ou stop non positif"
    distance = abs(entry - stop)
    if distance == 0:
        return "stop égal à l'entrée"
    per_unit_loss = distance + entry * 2 * cfg.fee_taker
    qty = equity * cfg.risk_per_trade / per_unit_loss
    max_qty_leverage = cfg.max_effective_leverage * equity / entry
    qty = min(qty, max_qty_leverage)
    rounded = rules.floor_qty(qty)
    if not rules.is_tradeable(rounded, entry):
        return f"quantité {rounded} sous le minimum Bybit ({rules.min_qty})"
    final = float(rounded)
    return Size(
        qty=final,
        effective_leverage=final * entry / equity,
        risk_usdt=final * per_unit_loss,
    )
