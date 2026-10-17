"""Plafonds de risque codés en dur et configuration (qui ne peut que les abaisser).

Ces constantes ne se règlent ni par variable d'environnement, ni depuis le
dashboard. Une configuration qui tente de les dépasser est refusée.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import timedelta
from typing import Final

# --- Plafonds non négociables (SPEC, « Gestion du risque ») -----------------
MAX_RISK_PER_TRADE: Final = 0.02
MAX_EFFECTIVE_LEVERAGE: Final = 3.0
MAX_EXCHANGE_LEVERAGE: Final = 3
MAX_OPEN_POSITIONS: Final = 3
MAX_TOTAL_OPEN_RISK: Final = 0.03
MAX_DAILY_LOSS: Final = 0.03
MAX_WEEKLY_LOSS: Final = 0.06
MAX_DRAWDOWN: Final = 0.15
MAX_CONSECUTIVE_LOSSES: Final = 4
MIN_LIQ_DISTANCE_MULTIPLE: Final = 3.0  # liquidation ≥ 3 × distance au stop
CONSECUTIVE_LOSS_PAUSE: Final = timedelta(hours=24)
STOP_CONFIRM_TIMEOUT_S: Final = 5.0

_CAPS: Final[dict[str, float]] = {
    "risk_per_trade": MAX_RISK_PER_TRADE,
    "max_effective_leverage": MAX_EFFECTIVE_LEVERAGE,
    "exchange_leverage": MAX_EXCHANGE_LEVERAGE,
    "max_open_positions": MAX_OPEN_POSITIONS,
    "max_total_open_risk": MAX_TOTAL_OPEN_RISK,
    "max_daily_loss": MAX_DAILY_LOSS,
    "max_weekly_loss": MAX_WEEKLY_LOSS,
    "max_drawdown": MAX_DRAWDOWN,
    "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
}


class RiskConfigError(ValueError):
    """Paramètre de risque au-delà d'un plafond codé en dur."""


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.01
    max_effective_leverage: float = 2.0
    exchange_leverage: int = 3
    max_open_positions: int = 2
    max_total_open_risk: float = 0.02
    max_daily_loss: float = 0.03
    max_weekly_loss: float = 0.06
    max_drawdown: float = 0.15
    max_consecutive_losses: int = 4
    fee_taker: float = 0.00055
    maintenance_margin_rate: float = 0.005

    def __post_init__(self) -> None:
        for name, cap in _CAPS.items():
            value = getattr(self, name)
            if not 0 < value <= cap:
                raise RiskConfigError(f"{name}={value} hors de ]0, {cap}] (plafond codé en dur)")
        if not 0 <= self.fee_taker < 0.01:
            raise RiskConfigError(f"fee_taker={self.fee_taker} invraisemblable")
        if not 0 <= self.maintenance_margin_rate < 0.1:
            raise RiskConfigError(
                f"maintenance_margin_rate={self.maintenance_margin_rate} invraisemblable"
            )

    def as_table(self) -> list[dict[str, str | float | None]]:
        """Paramètres et plafonds, pour l'affichage en lecture seule."""
        return [
            {"name": f.name, "value": getattr(self, f.name), "cap": _CAPS.get(f.name)}
            for f in fields(self)
        ]
