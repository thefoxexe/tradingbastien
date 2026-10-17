from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from bot.config import Settings
from bot.exchange.instruments import DEFAULT_RULES, InstrumentRules
from bot.models import BotStatus, Position, Side, Signal
from bot.risk import limits
from bot.risk.limits import RiskConfig, RiskConfigError
from bot.risk.manager import HALT_CONFIRMATION, RiskManager, RiskState, tighten_stop
from bot.risk.sizing import Size, compute_size, liquidation_price

BTC = DEFAULT_RULES["BTCUSDT"]
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)  # mercredi
FINE = InstrumentRules(
    "BTCUSDT", Decimal("0.01"), Decimal("0.000001"), Decimal("0.000001"), Decimal(10**9)
)


def sig(
    entry: float = 60_000, stop: float = 58_800, side: Side = Side.LONG, symbol: str = "BTCUSDT"
) -> Signal:
    return Signal(symbol, side, entry, stop, NOW)


def pos(
    symbol: str = "ETHUSDT", entry: float = 3000, stop: float = 2900, qty: float = 0.1
) -> Position:
    return Position(symbol, Side.LONG, qty, entry, stop, stop, NOW)


def status(m: RiskManager) -> BotStatus:
    """Lecture non rétrécie par mypy (le statut change entre deux appels)."""
    return m.state.status


def manager(**cfg: float) -> RiskManager:
    m = RiskManager(RiskConfig(**cfg))  # type: ignore[arg-type]
    m.update_equity(1000, NOW)
    return m


# ---------------------------------------------------------------- plafonds
@pytest.mark.parametrize(
    ("name", "too_high"),
    [
        ("risk_per_trade", 0.021),
        ("max_effective_leverage", 3.01),
        ("exchange_leverage", 4),
        ("max_open_positions", 4),
        ("max_total_open_risk", 0.031),
        ("max_daily_loss", 0.031),
        ("max_weekly_loss", 0.061),
        ("max_drawdown", 0.151),
        ("max_consecutive_losses", 5),
    ],
)
def test_config_cannot_raise_caps(name: str, too_high: float) -> None:
    with pytest.raises(RiskConfigError):
        RiskConfig(**{name: too_high})  # type: ignore[arg-type]
    with pytest.raises(RiskConfigError):
        RiskConfig(**{name: 0})


def test_config_can_lower_caps() -> None:
    cfg = RiskConfig(risk_per_trade=0.005, max_effective_leverage=1, max_open_positions=1)
    assert cfg.risk_per_trade == 0.005
    assert {row["name"] for row in cfg.as_table()} >= {"risk_per_trade", "fee_taker"}


@pytest.mark.parametrize(("field", "value"), [("fee_taker", 0.5), ("maintenance_margin_rate", 0.5)])
def test_config_rejects_absurd_fees(field: str, value: float) -> None:
    with pytest.raises(RiskConfigError):
        RiskConfig(**{field: value})  # type: ignore[arg-type]


def test_env_cannot_raise_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_PER_TRADE", "0.05")
    with pytest.raises(ValidationError, match="plafond"):
        Settings()


def test_hard_caps_match_spec() -> None:
    assert limits.MAX_RISK_PER_TRADE == 0.02
    assert limits.MAX_EFFECTIVE_LEVERAGE == 3
    assert limits.MAX_EXCHANGE_LEVERAGE == 3
    assert limits.MAX_OPEN_POSITIONS == 3
    assert limits.MAX_TOTAL_OPEN_RISK == 0.03
    assert limits.MAX_DAILY_LOSS == 0.03
    assert limits.MAX_WEEKLY_LOSS == 0.06
    assert limits.MAX_DRAWDOWN == 0.15
    assert limits.MAX_CONSECUTIVE_LOSSES == 4
    assert limits.MIN_LIQ_DISTANCE_MULTIPLE == 3


# ---------------------------------------------------------------- sizing
def test_spec_example_without_fees() -> None:
    size = compute_size(1000, 60_000, 58_800, RiskConfig(fee_taker=0), BTC)
    assert isinstance(size, Size)
    assert size.qty == 0.008  # 0,00833 arrondi vers le bas au pas de 0,001
    assert size.effective_leverage == pytest.approx(0.48)
    assert size.risk_usdt <= 10


def test_fees_reduce_size() -> None:
    no_fee = compute_size(100_000, 60_000, 58_800, RiskConfig(fee_taker=0), BTC)
    fee = compute_size(100_000, 60_000, 58_800, RiskConfig(), BTC)
    assert isinstance(no_fee, Size) and isinstance(fee, Size)
    assert fee.qty < no_fee.qty


def test_very_close_stop_capped_by_leverage() -> None:
    size = compute_size(10_000, 60_000, 59_999, RiskConfig(max_effective_leverage=2), BTC)
    assert isinstance(size, Size)
    assert size.effective_leverage <= 2


def test_minimal_capital_refused() -> None:
    assert isinstance(compute_size(20, 60_000, 58_800, RiskConfig(), BTC), str)


@pytest.mark.parametrize(
    ("equity", "entry", "stop"), [(0, 1, 2), (1000, 0, 1), (1000, 1, 0), (1000, 5, 5)]
)
def test_invalid_inputs_refused(equity: float, entry: float, stop: float) -> None:
    assert isinstance(compute_size(equity, entry, stop, RiskConfig(), BTC), str)


def test_liquidation_price() -> None:
    assert liquidation_price(100, long=True, leverage=3, mmr=0) == pytest.approx(66.6667, rel=1e-4)
    assert liquidation_price(100, long=False, leverage=3, mmr=0) == pytest.approx(133.333, rel=1e-4)


# ---------------------------------------------------------------- veto
def test_approved_decision() -> None:
    d = manager().evaluate(sig(), equity=1000, positions=[], rules=BTC, now=NOW)
    assert d.approved and d.qty == 0.007 and d.stop == 58_800  # 10 / (1200 + 66 de frais)
    assert d.risk_usdt <= 10


def test_short_decision() -> None:
    d = manager().evaluate(
        sig(60_000, 61_200, Side.SHORT), equity=1000, positions=[], rules=BTC, now=NOW
    )
    assert d.approved and d.stop == 61_200


@pytest.mark.parametrize(
    ("signal", "reason"),
    [
        (sig(stop=61_000), "mauvais côté"),
        (sig(60_000, 59_000, Side.SHORT), "mauvais côté"),
        (sig(entry=0), "mauvais côté"),
        (sig(symbol="PEPEUSDT"), "hors périmètre"),
        (sig(stop=59_999.95), "après arrondi"),
        (sig(stop=30_000), "liquidation"),
    ],
)
def test_vetoes(signal: Signal, reason: str) -> None:
    d = manager().evaluate(signal, equity=100_000, positions=[], rules=BTC, now=NOW)
    assert not d.approved and reason in d.reason


def test_veto_when_unhealthy() -> None:
    d = manager().evaluate(sig(), equity=1000, positions=[], rules=BTC, now=NOW, healthy=False)
    assert not d.approved and "incertain" in d.reason


def test_veto_existing_position_on_pair() -> None:
    d = manager().evaluate(sig(), equity=1000, positions=[pos("BTCUSDT")], rules=BTC, now=NOW)
    assert not d.approved and "déjà" in d.reason


def test_veto_max_positions() -> None:
    m = manager(max_open_positions=1)
    d = m.evaluate(sig(), equity=1000, positions=[pos()], rules=BTC, now=NOW)
    assert not d.approved and "maximum" in d.reason


def test_veto_total_open_risk() -> None:
    m = manager()
    d = m.evaluate(
        sig(), equity=1000, positions=[pos(qty=0.15)], rules=BTC, now=NOW
    )  # 15 USDT ouverts
    assert not d.approved and "risque total" in d.reason


def test_veto_sizing_reason() -> None:
    d = manager().evaluate(sig(), equity=20, positions=[], rules=BTC, now=NOW)
    assert not d.approved and "minimum" in d.reason


def test_max_capital_limits_sizing() -> None:
    m = manager()
    m.max_capital = 1000
    d = m.evaluate(sig(), equity=50_000, positions=[], rules=BTC, now=NOW)
    assert d.approved and d.risk_usdt <= 10


# ---------------------------------------------------------------- limites de perte
def test_daily_loss_pauses_until_midnight() -> None:
    m = manager()
    m.update_equity(970, NOW)
    assert status(m) is BotStatus.PAUSE
    assert m.state.pause_until == datetime(2026, 10, 15, tzinfo=UTC)
    assert not m.evaluate(sig(), equity=970, positions=[], rules=BTC, now=NOW).approved
    assert m.refresh(datetime(2026, 10, 15, 0, 1, tzinfo=UTC)) is BotStatus.RUN
    assert m.daily_pnl_pct() == pytest.approx(-0.03)


def test_weekly_loss_pauses_until_monday() -> None:
    m = manager()
    m.update_equity(980, NOW)
    m.update_equity(975, NOW + timedelta(days=1))  # nouveau jour, base 975
    m.update_equity(940, NOW + timedelta(days=2))  # -4 % jour, -6 % semaine
    assert status(m) is BotStatus.PAUSE
    assert m.state.pause_until == datetime(2026, 10, 19, tzinfo=UTC)
    assert "hebdomadaire" in m.state.status_reason
    assert m.weekly_pnl_pct() == pytest.approx(-0.06)


def test_drawdown_halts() -> None:
    m = manager()
    m.update_equity(1200, NOW)
    for i, eq in enumerate([1180, 1150, 1120, 1080, 1050, 1020]):
        m.update_equity(eq, NOW + timedelta(days=7 * (i + 1)))
        m.refresh(NOW + timedelta(days=7 * (i + 1), hours=23))
    assert status(m) is BotStatus.HALT
    assert m.drawdown() == pytest.approx(0.15)
    m.update_equity(1300, NOW + timedelta(days=100))
    assert status(m) is BotStatus.HALT  # seule une confirmation manuelle sort de HALT


def test_consecutive_losses_pause_24h() -> None:
    m = manager()
    m.record_trade(-1, NOW)
    m.record_trade(5, NOW)
    for _ in range(4):
        m.record_trade(-1, NOW)
    assert status(m) is BotStatus.PAUSE
    assert m.state.pause_until == NOW + timedelta(hours=24)


def test_pause_never_shortened() -> None:
    m = manager()
    for _ in range(4):
        m.record_trade(-1, NOW)  # pause jusqu'à demain midi
    m.update_equity(960, NOW)  # perte jour : pause jusqu'à minuit, plus courte
    assert m.state.pause_until == NOW + timedelta(hours=24)


def test_pause_can_be_extended() -> None:
    m = manager()
    m.update_equity(960, NOW)  # pause jusqu'à minuit
    for _ in range(4):
        m.record_trade(-1, NOW)  # 24 h : plus long, remplace
    assert m.state.pause_until == NOW + timedelta(hours=24)


def test_manual_pause_resume_and_kill() -> None:
    m = manager()
    m.pause()
    assert status(m) is BotStatus.PAUSE and m.state.pause_until is None
    m.update_equity(960, NOW)  # pause auto ne remplace pas la pause manuelle
    assert m.state.pause_until is None
    m.resume()
    assert status(m) is BotStatus.RUN
    m.kill()
    m.pause()
    m.resume()
    assert status(m) is BotStatus.HALT
    assert not m.clear_halt("oui")
    assert m.clear_halt(HALT_CONFIRMATION)
    assert status(m) is BotStatus.PAUSE
    assert not m.clear_halt(HALT_CONFIRMATION)
    assert m.events


def test_state_roundtrip() -> None:
    m = manager()
    m.update_equity(960, NOW)
    restored = RiskState.from_dict(m.state.to_dict())
    assert restored == m.state
    assert RiskState.from_dict({}) == RiskState()


def test_empty_state_metrics() -> None:
    m = RiskManager(RiskConfig())
    assert m.drawdown() == m.daily_pnl_pct() == m.weekly_pnl_pct() == 0


# ---------------------------------------------------------------- stops
def test_stop_only_tightens() -> None:
    long = pos("BTCUSDT", 60_000, 58_800)
    assert tighten_stop(long, 59_000, 61_000) == 59_000
    assert tighten_stop(long, 58_000, 61_000) is None  # s'éloigne
    assert tighten_stop(long, 61_500, 61_000) is None  # au-dessus du prix
    short = replace(long, side=Side.SHORT, stop=61_200)
    assert tighten_stop(short, 61_000, 59_000) == 61_000
    assert tighten_stop(short, 62_000, 59_000) is None
    assert tighten_stop(short, 58_000, 59_000) is None


def test_position_metrics() -> None:
    p = pos("BTCUSDT", 100, 90, 2)
    assert p.open_risk == 20
    assert p.r_multiple(110) == 1
    p.stop = 105
    assert p.open_risk == 0
    assert replace(p, initial_stop=100).r_multiple(120) == 0


# ---------------------------------------------------------------- propriétés
configs = st.builds(
    RiskConfig,
    risk_per_trade=st.floats(0.001, limits.MAX_RISK_PER_TRADE),
    max_effective_leverage=st.floats(0.5, limits.MAX_EFFECTIVE_LEVERAGE),
    max_open_positions=st.integers(1, limits.MAX_OPEN_POSITIONS),
    max_total_open_risk=st.floats(0.005, limits.MAX_TOTAL_OPEN_RISK),
    fee_taker=st.floats(0, 0.001),
)


@settings(max_examples=500, deadline=None)
@given(
    cfg=configs,
    equity=st.floats(10, 1e7),
    entry=st.floats(1, 200_000),
    stop_pct=st.floats(0.0001, 0.5),
    long=st.booleans(),
    open_risks=st.lists(st.floats(0, 100), max_size=3),
)
def test_no_approved_decision_exceeds_caps(
    cfg: RiskConfig,
    equity: float,
    entry: float,
    stop_pct: float,
    long: bool,
    open_risks: list[float],
) -> None:
    stop = entry * (1 - stop_pct) if long else entry * (1 + stop_pct)
    m = RiskManager(cfg)
    m.update_equity(equity, NOW)
    positions = [
        Position("ETHUSDT", Side.LONG, r / 10 if r else 0, 100, 90, 90, NOW) for r in open_risks[:1]
    ]
    side = Side.LONG if long else Side.SHORT
    d = m.evaluate(
        Signal("BTCUSDT", side, entry, stop, NOW),
        equity=equity,
        positions=positions,
        rules=FINE,
        now=NOW,
    )
    if not d.approved:
        return
    eps = 1e-9
    assert d.qty > 0
    assert d.risk_usdt <= equity * cfg.risk_per_trade * (1 + eps)
    assert d.risk_usdt <= equity * limits.MAX_RISK_PER_TRADE * (1 + eps)
    assert d.effective_leverage <= cfg.max_effective_leverage * (1 + eps)
    assert d.effective_leverage <= limits.MAX_EFFECTIVE_LEVERAGE * (1 + eps)
    assert sum(
        p.open_risk for p in positions
    ) + d.risk_usdt <= equity * limits.MAX_TOTAL_OPEN_RISK * (1 + eps)
    assert len(positions) < cfg.max_open_positions
    assert abs(entry - d.liq_price) >= limits.MIN_LIQ_DISTANCE_MULTIPLE * abs(entry - d.stop) * (
        1 - eps
    )
    assert (d.stop < entry) if long else (d.stop > entry)
    # le risque réel au stop (avec frais) ne dépasse pas la limite
    real = d.qty * (abs(entry - d.stop) + entry * 2 * cfg.fee_taker)
    assert real <= equity * cfg.risk_per_trade * (1 + eps)


@settings(max_examples=300, deadline=None)
@given(equities=st.lists(st.floats(1, 1e6), min_size=1, max_size=40))
def test_halt_is_sticky(equities: list[float]) -> None:
    m = RiskManager(RiskConfig())
    m.kill()
    for i, eq in enumerate(equities):
        m.update_equity(eq, NOW + timedelta(days=i))
        m.pause()
        m.resume()
        m.record_trade(eq - 500, NOW)
        assert status(m) is BotStatus.HALT
    assume(True)
