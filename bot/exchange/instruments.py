"""Règles d'instruments Bybit (``/v5/market/instruments-info``) et arrondis."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal = Decimal(0)
    max_leverage: Decimal = Decimal(100)

    @classmethod
    def from_bybit(cls, item: dict[str, Any]) -> InstrumentRules:
        lot = item["lotSizeFilter"]
        return cls(
            symbol=item["symbol"],
            tick_size=Decimal(item["priceFilter"]["tickSize"]),
            qty_step=Decimal(lot["qtyStep"]),
            min_qty=Decimal(lot["minOrderQty"]),
            max_qty=Decimal(lot["maxOrderQty"]),
            min_notional=Decimal(lot.get("minNotionalValue") or 0),
            max_leverage=Decimal(item.get("leverageFilter", {}).get("maxLeverage") or 100),
        )

    def floor_qty(self, qty: float) -> Decimal:
        """Arrondi vers le bas au ``qtyStep`` : la taille n'est jamais augmentée."""
        steps = (Decimal(str(qty)) / self.qty_step).to_integral_value(rounding=ROUND_DOWN)
        return min(steps * self.qty_step, self.max_qty).normalize()

    def round_price(self, price: float) -> Decimal:
        steps = (Decimal(str(price)) / self.tick_size).to_integral_value(rounding=ROUND_HALF_UP)
        return (steps * self.tick_size).normalize()

    def round_stop(self, stop: float, *, long: bool) -> Decimal:
        """Arrondi du stop vers le prix d'entrée (risque jamais augmenté par l'arrondi)."""
        rounding = ROUND_UP if long else ROUND_DOWN
        steps = (Decimal(str(stop)) / self.tick_size).to_integral_value(rounding=rounding)
        return (steps * self.tick_size).normalize()

    def is_tradeable(self, qty: Decimal, price: float) -> bool:
        return qty >= self.min_qty and qty * Decimal(str(price)) >= self.min_notional


# Règles de repli pour le backtest hors ligne (valeurs Bybit usuelles, à rafraîchir en démo/live).
DEFAULT_RULES: dict[str, InstrumentRules] = {
    "BTCUSDT": InstrumentRules(
        "BTCUSDT", Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("1190"), Decimal(5)
    ),
    "ETHUSDT": InstrumentRules(
        "ETHUSDT", Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), Decimal("7240"), Decimal(5)
    ),
}
