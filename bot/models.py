"""Types partagés par la stratégie, le risque, l'exécution et le backtest."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def bybit_entry(self) -> str:
        """Côté Bybit de l'ordre d'entrée."""
        return "Buy" if self is Side.LONG else "Sell"

    @property
    def bybit_exit(self) -> str:
        """Côté Bybit de l'ordre de sortie."""
        return "Sell" if self is Side.LONG else "Buy"


class BotStatus(StrEnum):
    RUN = "RUN"
    PAUSE = "PAUSE"
    HALT = "HALT"


@dataclass(frozen=True)
class Signal:
    """Ce qu'une stratégie renvoie : paire, sens, prix d'entrée, stop. Rien d'autre."""

    symbol: str
    side: Side
    entry: float
    stop: float
    bar_time: datetime
    strategy: str = "trend"

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def link_id(self) -> str:
        """``orderLinkId`` unique par signal (évite les doublons en cas de retry)."""
        return f"{self.strategy[:8]}-{self.symbol}-{self.side.value[0]}-{int(self.bar_time.timestamp())}"


@dataclass
class Position:
    """Position ouverte, telle que connue du bot (Bybit fait toujours foi)."""

    symbol: str
    side: Side
    qty: float
    entry: float
    stop: float
    initial_stop: float
    opened_at: datetime
    strategy: str = "trend"
    liq_price: float | None = None
    mark_price: float | None = None
    unrealized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    max_favorable: float = field(default=0.0)

    @property
    def initial_risk_per_unit(self) -> float:
        return abs(self.entry - self.initial_stop)

    @property
    def open_risk(self) -> float:
        """Perte en USDT si le stop actuel est touché (0 si le stop protège un gain)."""
        loss_per_unit = (self.entry - self.stop) * self.side.sign
        return max(loss_per_unit, 0.0) * self.qty

    def r_multiple(self, price: float) -> float:
        risk = self.initial_risk_per_unit
        if risk == 0:
            return 0.0
        return (price - self.entry) * self.side.sign / risk


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    side: Side
    qty: float
    entry: float
    exit: float
    initial_stop: float
    opened_at: datetime
    closed_at: datetime
    fees: float
    funding: float
    exit_reason: str
    strategy: str = "trend"

    @property
    def gross_pnl(self) -> float:
        return (self.exit - self.entry) * self.side.sign * self.qty

    @property
    def net_pnl(self) -> float:
        """P&L réel : frais et funding compris (funding positif = payé)."""
        return self.gross_pnl - self.fees - self.funding

    @property
    def risk_usdt(self) -> float:
        return abs(self.entry - self.initial_stop) * self.qty

    @property
    def r_multiple(self) -> float:
        return self.net_pnl / self.risk_usdt if self.risk_usdt else 0.0
