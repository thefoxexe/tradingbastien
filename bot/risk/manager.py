"""Module de risque : seul passage vers l'exécution, avec droit de veto.

Il calcule la taille, applique les plafonds et les limites de perte, gère
les pauses et le mode HALT (kill switch). La même classe sert en backtest,
en démo et en live.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from bot.config import ALLOWED_SYMBOLS
from bot.exchange.instruments import InstrumentRules
from bot.models import BotStatus, Position, Side, Signal
from bot.risk.limits import (
    CONSECUTIVE_LOSS_PAUSE,
    MIN_LIQ_DISTANCE_MULTIPLE,
    RiskConfig,
)
from bot.risk.sizing import Size, compute_size, liquidation_price

HALT_CONFIRMATION = "JE CONFIRME LA SORTIE DU MODE HALT"


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str
    signal: Signal
    qty: float = 0.0
    stop: float = 0.0
    effective_leverage: float = 0.0
    risk_usdt: float = 0.0
    liq_price: float = 0.0


@dataclass
class RiskState:
    """État persistant du module de risque (sauvé en base à chaque changement)."""

    status: BotStatus = BotStatus.RUN
    status_reason: str = ""
    pause_until: datetime | None = None
    peak_equity: float = 0.0
    day: date | None = None
    day_start_equity: float = 0.0
    week: str = ""
    week_start_equity: float = 0.0
    consecutive_losses: int = 0
    last_equity: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["status"] = self.status.value
        out["pause_until"] = self.pause_until.isoformat() if self.pause_until else None
        out["day"] = self.day.isoformat() if self.day else None
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RiskState:
        return cls(
            status=BotStatus(raw.get("status", "RUN")),
            status_reason=str(raw.get("status_reason", "")),
            pause_until=datetime.fromisoformat(raw["pause_until"])
            if raw.get("pause_until")
            else None,
            peak_equity=float(raw.get("peak_equity", 0.0)),
            day=date.fromisoformat(raw["day"]) if raw.get("day") else None,
            day_start_equity=float(raw.get("day_start_equity", 0.0)),
            week=str(raw.get("week", "")),
            week_start_equity=float(raw.get("week_start_equity", 0.0)),
            consecutive_losses=int(raw.get("consecutive_losses", 0)),
            last_equity=float(raw.get("last_equity", 0.0)),
        )


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _next_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)


def _next_monday(now: datetime) -> datetime:
    days = 7 - now.weekday()
    return datetime.combine(now.date() + timedelta(days=days), datetime.min.time(), UTC)


@dataclass
class RiskManager:
    cfg: RiskConfig
    state: RiskState = field(default_factory=RiskState)
    max_capital: float | None = None
    events: list[str] = field(default_factory=list)  # alertes à envoyer, vidées par l'appelant

    # ------------------------------------------------------------------ statut
    def _set(self, status: BotStatus, reason: str, until: datetime | None = None) -> None:
        if self.state.status is BotStatus.HALT and status is not BotStatus.HALT:
            return  # seul clear_halt sort du mode HALT
        if status is BotStatus.PAUSE and self.state.status is BotStatus.PAUSE:
            current = self.state.pause_until
            if current is None or (until is not None and until <= current):
                return  # une pause n'est jamais raccourcie
        if (
            self.state.status is status
            and self.state.pause_until == until
            and self.state.status_reason == reason
        ):
            return
        self.state.status = status
        self.state.status_reason = reason
        self.state.pause_until = until
        self.events.append(f"{status.value} : {reason}")

    def refresh(self, now: datetime) -> BotStatus:
        """Lève une pause automatique échue. Les pauses manuelles (sans échéance) restent."""
        s = self.state
        if s.status is BotStatus.PAUSE and s.pause_until is not None and now >= s.pause_until:
            s.status, s.status_reason, s.pause_until = BotStatus.RUN, "", None
            self.events.append("RUN : fin de pause automatique")
        return s.status

    def pause(self, reason: str = "pause manuelle") -> None:
        if self.state.status is BotStatus.RUN:
            self._set(BotStatus.PAUSE, reason)

    def resume(self) -> None:
        """Reprise après une pause manuelle (pas depuis HALT)."""
        if self.state.status is BotStatus.PAUSE:
            self.state.status, self.state.status_reason, self.state.pause_until = (
                BotStatus.RUN,
                "",
                None,
            )
            self.events.append("RUN : reprise manuelle")

    def kill(self, reason: str = "kill switch") -> None:
        self._set(BotStatus.HALT, reason)

    def clear_halt(self, confirmation: str) -> bool:
        if confirmation != HALT_CONFIRMATION or self.state.status is not BotStatus.HALT:
            return False
        self.state.status, self.state.status_reason, self.state.pause_until = (
            BotStatus.PAUSE,
            "sortie de HALT",
            None,
        )
        self.events.append("PAUSE : sortie de HALT confirmée, reprise manuelle requise")
        return True

    # ------------------------------------------------------------------ equity et limites
    def update_equity(self, equity: float, now: datetime) -> None:
        s = self.state
        today, week = now.date(), _iso_week(now.date())
        if s.day != today:
            s.day, s.day_start_equity = today, equity
        if s.week != week:
            s.week, s.week_start_equity = week, equity
        s.peak_equity = max(s.peak_equity, equity)
        s.last_equity = equity
        self.refresh(now)

        if s.peak_equity > 0 and equity <= s.peak_equity * (1 - self.cfg.max_drawdown):
            self._set(
                BotStatus.HALT,
                f"drawdown ≥ {self.cfg.max_drawdown:.0%} depuis le pic : arrêt complet",
            )
            return
        if s.week_start_equity > 0 and equity <= s.week_start_equity * (
            1 - self.cfg.max_weekly_loss
        ):
            self._set(
                BotStatus.PAUSE,
                f"perte hebdomadaire ≥ {self.cfg.max_weekly_loss:.0%} : pause jusqu'à lundi, revue requise",
                _next_monday(now),
            )
            return
        if s.day_start_equity > 0 and equity <= s.day_start_equity * (1 - self.cfg.max_daily_loss):
            self._set(
                BotStatus.PAUSE,
                f"perte journalière ≥ {self.cfg.max_daily_loss:.0%} : pause jusqu'à minuit UTC",
                _next_midnight(now),
            )

    def record_trade(self, net_pnl: float, now: datetime) -> None:
        if net_pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            self._set(
                BotStatus.PAUSE,
                f"{self.state.consecutive_losses} pertes consécutives : pause 24 h",
                now + CONSECUTIVE_LOSS_PAUSE,
            )
            self.state.consecutive_losses = 0

    def drawdown(self) -> float:
        s = self.state
        return 0.0 if s.peak_equity <= 0 else max(0.0, 1 - s.last_equity / s.peak_equity)

    def daily_pnl_pct(self) -> float:
        s = self.state
        return 0.0 if s.day_start_equity <= 0 else s.last_equity / s.day_start_equity - 1

    def weekly_pnl_pct(self) -> float:
        s = self.state
        return 0.0 if s.week_start_equity <= 0 else s.last_equity / s.week_start_equity - 1

    # ------------------------------------------------------------------ veto
    def evaluate(
        self,
        signal: Signal,
        *,
        equity: float,
        positions: Iterable[Position],
        rules: InstrumentRules,
        now: datetime,
        healthy: bool = True,
    ) -> Decision:
        """Taille la position ou oppose un veto. ``healthy=False`` : doute → rien."""

        def veto(reason: str) -> Decision:
            return Decision(False, reason, signal)

        open_positions = list(positions)
        if self.refresh(now) is not BotStatus.RUN:
            return veto(f"statut {self.state.status.value} : {self.state.status_reason}")
        if not healthy:
            return veto("état incertain (données, API ou réconciliation) : aucune entrée")
        if signal.symbol not in ALLOWED_SYMBOLS or rules.symbol != signal.symbol:
            return veto(f"paire hors périmètre : {signal.symbol}")
        if any(p.symbol == signal.symbol for p in open_positions):
            return veto("une position est déjà ouverte sur cette paire (pas de renforcement)")
        if len(open_positions) >= self.cfg.max_open_positions:
            return veto(f"{len(open_positions)} positions ouvertes : maximum atteint")

        long = signal.side is Side.LONG
        if (
            signal.entry <= 0
            or (long and signal.stop >= signal.entry)
            or (not long and signal.stop <= signal.entry)
        ):
            return veto("stop du mauvais côté de l'entrée")

        stop = float(rules.round_stop(signal.stop, long=long))
        if (long and stop >= signal.entry) or (not long and stop <= signal.entry):
            return veto("stop trop proche de l'entrée après arrondi au tick")

        capital = min(equity, self.max_capital) if self.max_capital else equity
        size = compute_size(capital, signal.entry, stop, self.cfg, rules)
        if not isinstance(size, Size):
            return veto(size)

        liq = liquidation_price(
            signal.entry,
            long=long,
            leverage=self.cfg.exchange_leverage,
            mmr=self.cfg.maintenance_margin_rate,
        )
        if abs(signal.entry - liq) < MIN_LIQ_DISTANCE_MULTIPLE * abs(signal.entry - stop):
            return veto("liquidation à moins de 3 fois la distance au stop")

        open_risk = sum(p.open_risk for p in open_positions)
        if open_risk + size.risk_usdt > self.cfg.max_total_open_risk * capital:
            return veto("risque total ouvert dépassé")

        return Decision(
            True,
            "ok",
            signal,
            qty=size.qty,
            stop=stop,
            effective_leverage=size.effective_leverage,
            risk_usdt=size.risk_usdt,
            liq_price=liq,
        )


def tighten_stop(position: Position, proposed: float, price: float) -> float | None:
    """Nouveau stop seulement s'il se rapproche (jamais il ne s'éloigne) et reste valide."""
    if position.side is Side.LONG:
        if proposed > position.stop and proposed < price:
            return proposed
    elif proposed < position.stop and proposed > price:
        return proposed
    return None
