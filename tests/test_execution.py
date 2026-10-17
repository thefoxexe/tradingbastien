from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from bot.admin import main as admin_main
from bot.alerts import TelegramNotifier
from bot.config import Settings
from bot.data.candles import BAR
from bot.exchange.client import BybitClient, ExchangeError, ExchangePosition
from bot.exchange.instruments import InstrumentRules
from bot.exchange.permissions import ForbiddenPermissionError
from bot.execution.executor import Executor
from bot.execution.reconcile import reconcile
from bot.main import build_bot
from bot.models import BotStatus, ClosedTrade, Position, Side, Signal
from bot.risk.limits import RiskConfig
from bot.risk.manager import HALT_CONFIRMATION, Decision, RiskManager
from bot.runner import Bot, http_heartbeat
from bot.store import Store
from bot.strategy.base import Management
from tests.fakes import INSTRUMENT, FakeBybitError, FakeSession

NOW = datetime(2026, 10, 14, 12, 0, 5, tzinfo=UTC)
RULES = {s: InstrumentRules.from_bybit(dict(INSTRUMENT, symbol=s)) for s in ("BTCUSDT", "ETHUSDT")}


class Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, text: str, *, level: str = "INFO", kind: str = "info") -> None:
        self.messages.append((level, kind, text))

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.messages]


def setup(
    session: FakeSession | None = None,
) -> tuple[FakeSession, BybitClient, RiskManager, Executor, Recorder]:
    s = session or FakeSession()
    c = BybitClient(s, sleep=lambda _: None)
    risk = RiskManager(RiskConfig())
    risk.update_equity(1000, NOW)
    rec = Recorder()
    ex = Executor(c, risk, RULES, rec, fee_taker=0.00055, sleep=lambda _: None, clock=lambda: NOW)
    return s, c, risk, ex, rec


def approved(entry: float = 60_000, stop: float = 58_800, side: Side = Side.LONG) -> Decision:
    sig = Signal("BTCUSDT", side, entry, stop, NOW)
    return Decision(True, "ok", sig, qty=0.007, stop=stop, risk_usdt=9)


# ---------------------------------------------------------------- exécution
def test_open_with_stop_confirmed() -> None:
    s, _, _, ex, rec = setup()
    pos = ex.open(approved())
    assert pos is not None and pos.stop == 58_800 and pos.qty == 0.007
    assert s.calls[[n for n, _ in s.calls].index("place_order")][1]["stopLoss"] == "58800"
    assert "entry" in rec.kinds()


def test_open_refuses_unapproved_decision() -> None:
    _, _, _, ex, _ = setup()
    with pytest.raises(ValueError, match="non approuvée"):
        ex.open(Decision(False, "veto", approved().signal))


def test_position_without_stop_closed_after_5s() -> None:
    s, c, _, ex, rec = setup()
    s.drop_stop = True
    assert ex.open(approved()) is None
    assert c.positions() == []  # fermée au marché
    assert any(level == "CRITICAL" for level, _, _ in rec.messages)


def test_no_position_visible_after_order() -> None:
    class Ghost(FakeSession):
        def place_order(self, **kw: object) -> dict[str, object]:
            self._log("place_order", dict(kw))
            return {"retCode": 0, "result": {"orderId": "x"}}

    _, _, _, ex, rec = setup(Ghost())
    assert ex.open(approved()) is None
    assert rec.messages[-1][0] == "ERROR"


def test_partial_fill_uses_real_quantity() -> None:
    s, _, _, ex, rec = setup()
    s.fill_ratio = 0.5
    pos = ex.open(approved())
    assert pos is not None and pos.qty == pytest.approx(0.0035, abs=1e-3)
    assert any("partiel" in t for _, _, t in rec.messages)


def test_move_stop_only_tightens_and_rounds() -> None:
    s, _, _, ex, _ = setup()
    pos = ex.open(approved())
    assert pos is not None
    moved = ex.move_stop(pos, 59_500.04, 61_000)
    assert moved.stop == 59_500.1 and s.positions["BTCUSDT"]["stopLoss"] == 59_500.1
    assert ex.move_stop(moved, 59_000, 61_000) is moved  # jamais d'éloignement
    # arrondi qui franchirait le prix : on arrondit dans l'autre sens
    near = ex.move_stop(moved, 60_999.95, 61_000)
    assert near.stop == 60_999.9
    tight = replace(moved, stop=60_999.9)
    assert ex.move_stop(tight, 60_999.95, 61_000) is tight


def test_move_stop_short() -> None:
    _s, _, _, ex, _ = setup()
    pos = ex.open(approved(60_000, 61_200, Side.SHORT))
    assert pos is not None
    assert ex.move_stop(pos, 60_500, 59_000).stop == 60_500


def test_closed_trade_uses_bybit_closed_pnl() -> None:
    class S(FakeSession):
        def get_closed_pnl(self, **kw: object) -> dict[str, object]:
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "avgExitPrice": "61000",
                            "closedPnl": "6.0",
                            "updatedTime": str(int(NOW.timestamp() * 1000)),
                        }
                    ]
                },
            }

    _, _, _, ex, _ = setup(S())
    pos = Position("BTCUSDT", Side.LONG, 0.007, 60_000, 58_800, 58_800, NOW, fees=0.23)
    t = ex.closed_trade(pos, "stop")
    assert t.exit == 61_000 and t.net_pnl == pytest.approx(6.0)


def test_closed_trade_ignores_older_rows_and_falls_back_to_mark() -> None:
    class S(FakeSession):
        def get_closed_pnl(self, **kw: object) -> dict[str, object]:
            return {
                "retCode": 0,
                "result": {"list": [{"avgExitPrice": "1", "closedPnl": "-99", "updatedTime": "1"}]},
            }

    s = S()
    s.marks["BTCUSDT"] = 59_000
    _, _, _, ex, _ = setup(s)
    t = ex.closed_trade(Position("BTCUSDT", Side.LONG, 0.01, 60_000, 58_800, 58_800, NOW), "x")
    assert t.exit == 59_000


def test_closed_trade_survives_api_errors() -> None:
    class S(FakeSession):
        def get_closed_pnl(self, **kw: object) -> dict[str, object]:
            raise FakeBybitError("bad", 10001)

        def get_tickers(self, **kw: object) -> dict[str, object]:
            raise FakeBybitError("bad", 10001)

    _, _, _, ex, _ = setup(S())
    pos = Position("BTCUSDT", Side.LONG, 0.01, 60_000, 58_800, 58_800, NOW, mark_price=59_500)
    assert ex.closed_trade(pos, "x").exit == 59_500


def test_kill_switch_cancels_closes_and_halts() -> None:
    s, c, risk, ex, rec = setup()
    ex.open(approved())
    s.orders.append({"symbol": "BTCUSDT"})
    ex.kill("test")
    assert c.positions() == [] and s.orders == []
    assert risk.state.status is BotStatus.HALT
    assert rec.messages[-1][1] == "kill"


def test_kill_switch_reports_errors() -> None:
    class S(FakeSession):
        def cancel_all_orders(self, **kw: object) -> dict[str, object]:
            raise FakeBybitError("x", 10001)

        def get_positions(self, **kw: object) -> dict[str, object]:
            raise FakeBybitError("x", 10001)

    _, _, risk, ex, rec = setup(S())
    ex.kill("test")
    assert risk.state.status is BotStatus.HALT
    assert rec.messages[-1][0] == "CRITICAL"

    class T(FakeSession):
        def place_order(self, **kw: object) -> dict[str, object]:
            if kw.get("reduceOnly"):
                raise FakeBybitError("x", 10001)
            return super().place_order(**kw)

    t = T()
    _, _, _, ex2, rec2 = setup(t)
    ex2.open(approved())
    ex2.kill("test")
    assert "clôture BTCUSDT" in rec2.messages[-1][2]


# ---------------------------------------------------------------- réconciliation
def xpos(
    stop: float | None = 58_800, qty: float = 0.007, side: Side = Side.LONG, sym: str = "BTCUSDT"
) -> ExchangePosition:
    return ExchangePosition(sym, side, qty, 60_000, stop, 40_000, 60_100, 0.7, 3, NOW)


def local(stop: float = 58_800, qty: float = 0.007) -> dict[str, Position]:
    return {"BTCUSDT": Position("BTCUSDT", Side.LONG, qty, 60_000, stop, 58_800, NOW)}


def test_reconcile_in_sync() -> None:
    r = reconcile(local(), [xpos()], [{"stopOrderType": "StopLoss"}], NOW)
    assert r.healthy and r.positions["BTCUSDT"].mark_price == 60_100


def test_reconcile_position_without_stop() -> None:
    r = reconcile({}, [xpos(stop=None)], [], NOW)
    assert not r.healthy and r.to_close


def test_reconcile_rebuilds_from_exchange() -> None:
    r = reconcile({}, [xpos()], [], NOW)
    assert r.healthy and r.positions["BTCUSDT"].initial_stop == 58_800 and r.notes


def test_reconcile_detects_closed_position() -> None:
    r = reconcile(local(), [], [], NOW)
    assert r.closed and not r.positions


def test_reconcile_side_mismatch_blocks() -> None:
    r = reconcile(local(), [xpos(side=Side.SHORT, stop=61_000)], [], NOW)
    assert not r.healthy and r.positions["BTCUSDT"].side is Side.SHORT


def test_reconcile_qty_and_stops() -> None:
    r = reconcile(local(qty=0.01), [xpos(qty=0.007)], [], NOW)
    assert r.positions["BTCUSDT"].qty == 0.007 and r.notes
    wider = reconcile(local(stop=59_500), [xpos(stop=58_800)], [], NOW)
    assert wider.stop_fixes == [("BTCUSDT", 59_500)] and wider.positions["BTCUSDT"].stop == 59_500
    closer = reconcile(local(stop=58_800), [xpos(stop=59_500)], [], NOW)
    assert not closer.stop_fixes and closer.positions["BTCUSDT"].stop == 59_500


def test_reconcile_unexpected_order_blocks() -> None:
    r = reconcile(
        {}, [], [{"symbol": "BTCUSDT", "side": "Buy", "orderLinkId": "?", "stopOrderType": ""}], NOW
    )
    assert not r.healthy
    ok = reconcile({}, [], [{"reduceOnly": True}], NOW)
    assert ok.healthy


# ---------------------------------------------------------------- store
def test_store_roundtrip(tmp_path: Path) -> None:
    st = Store(f"sqlite:///{tmp_path}/t.db")
    st.put("a", {"x": 1})
    st.put("a", {"x": 2})
    assert st.get("a") == {"x": 2} and st.get("nope", 5) == 5
    assert st.updated_at("a") is not None and st.updated_at("nope") is None
    p = Position("BTCUSDT", Side.LONG, 0.01, 60_000, 59_000, 58_800, NOW, liq_price=40_000)
    st.save_positions({"BTCUSDT": p})
    assert st.load_positions()["BTCUSDT"] == p
    t = ClosedTrade("BTCUSDT", Side.LONG, 0.01, 60_000, 61_000, 58_800, NOW, NOW, 0.5, 0.1, "stop")
    st.add_trade(t)
    assert st.list_trades()[0]["net_pnl"] == pytest.approx(t.net_pnl)
    st.log_event("x", "hello", "ERROR")
    assert st.list_events()[0]["message"] == "hello"
    st.record_equity(NOW, 1000)
    st.record_equity(NOW, 1001)
    assert st.equity_curve() == [(NOW, 1001)]
    st.push_command("pause", "test")
    st.push_command("kill", "test")
    assert st.pop_commands() == [("kill", "test"), ("pause", "test")]
    assert st.pop_commands() == []
    with pytest.raises(ValueError, match="refusée"):
        st.push_command("resume", "dashboard")


# ---------------------------------------------------------------- alertes
def test_telegram_notifier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import httpx

    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "body": request.content.decode()})
        return httpx.Response(200 if len(sent) == 1 else 500)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    st = Store(f"sqlite:///{tmp_path}/t.db")
    n = TelegramNotifier(
        Settings(), sink=st, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    n.send("bonjour")
    n.send("échec")  # erreur HTTP : loggée sans le token
    assert len(sent) == 2 and "[BACKTEST] bonjour" in sent[0]["body"]  # type: ignore[operator]
    assert [e["message"] for e in st.list_events()] == ["échec", "bonjour"]

    class Broken:
        def log_event(self, kind: str, message: str, level: str = "INFO") -> None:
            raise RuntimeError("db down")

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    TelegramNotifier(Settings(), sink=Broken()).send("sans telegram")


def test_http_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    calls: list[str] = []

    def fake_get(url: str, timeout: float) -> None:
        calls.append(url)
        if len(calls) > 1:
            raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", fake_get)
    ping = http_heartbeat("https://hc.example/ping")
    ping()
    ping()  # moins de 60 s : ignoré
    assert calls == ["https://hc.example/ping"]
    import time

    t = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    ping2 = http_heartbeat("u")
    ping2()
    t[0] += 61
    ping2()  # erreur réseau : avertissement, pas d'exception
    assert len(calls) == 3


# ---------------------------------------------------------------- boucle complète
def bars_rows(n: int = 300, end: datetime = NOW, breakout: bool = True) -> list[list[str]]:
    """Tendance haussière régulière ; la dernière bougie close casse le plus haut."""
    last_open = (
        datetime.fromtimestamp((end.timestamp() // BAR.total_seconds()) * BAR.total_seconds(), UTC)
        - BAR
    )
    idx = pd.date_range(end=last_open, periods=n, freq=BAR, tz=UTC)
    rows = []
    for i, t in enumerate(idx):
        c = 50_000 + i * 20
        if breakout and i == n - 1:
            c += 400
        rows.append(
            [
                str(int(t.timestamp() * 1000)),
                str(c - 10),
                str(c + 50),
                str(c - 50),
                str(c),
                "1",
                "1",
            ]
        )
    # bougie en cours (non close) : doit être ignorée
    current = last_open + BAR
    rows.append([str(int(current.timestamp() * 1000)), "1", "1", "1", "1", "1", "1"])
    return rows


def status(bot: Bot) -> BotStatus:
    return bot.risk.state.status


def make_bot(
    tmp_path: Path, session: FakeSession | None = None
) -> tuple[Bot, FakeSession, Recorder, Store]:
    s, c, risk, ex, rec = setup(session)
    st = Store(f"sqlite:///{tmp_path}/bot.db")
    from bot.strategy.trend import TrendFollowing

    bot = Bot(
        client=c, store=st, notifier=rec, risk=risk, executor=ex, strategy=TrendFollowing(),
        rules=RULES, symbols=["BTCUSDT"], mode="demo",
    )  # fmt: skip
    return bot, s, rec, st


def test_bot_full_cycle(tmp_path: Path) -> None:
    bot, s, rec, st = make_bot(tmp_path)
    s.klines["BTCUSDT"] = bars_rows()
    s.marks["BTCUSDT"] = 56_400
    bot.tick(NOW)
    assert "BTCUSDT" in bot.positions, rec.messages
    assert bot.positions["BTCUSDT"].stop < 56_400
    snap = st.get("snapshot")
    assert snap["status"] == "RUN" and snap["healthy"] and snap["equity"] == 1000

    # Même bougie : pas de nouveau signal.
    n_orders = sum(1 for n, _ in s.calls if n == "place_order")
    bot.tick(NOW + timedelta(seconds=5))
    assert sum(1 for n, _ in s.calls if n == "place_order") == n_orders

    # Le stop est touché chez Bybit : la réconciliation enregistre le trade.
    del s.positions["BTCUSDT"]
    bot.tick(NOW + timedelta(minutes=2))
    assert not bot.positions and st.list_trades()

    # Redémarrage : l'état est relu depuis la base.
    from bot.strategy.trend import TrendFollowing

    bot2 = Bot(
        client=bot.client, store=st, notifier=rec, risk=RiskManager(RiskConfig()), executor=bot.executor,
        strategy=TrendFollowing(), rules=RULES, symbols=["BTCUSDT"], mode="demo",
    )  # fmt: skip
    assert bot2.last_bar == bot.last_bar


def test_bot_no_late_entry_after_restart(tmp_path: Path) -> None:
    bot, s, _, st = make_bot(tmp_path)
    late = NOW + timedelta(hours=2)  # même bougie close, mais 2 h plus tard
    s.klines["BTCUSDT"] = bars_rows(end=late)
    s.marks["BTCUSDT"] = 56_400
    bot.tick(late)
    assert not bot.positions
    assert any("plus de 15 min" in e["message"] for e in st.list_events())


def test_bot_manages_open_position(tmp_path: Path) -> None:
    bot, s, _rec, st = make_bot(tmp_path)

    class Script:
        name = "trend"
        warmup = 1
        action = Management(new_stop=59_500)

        def signal(self, symbol: str, bars: pd.DataFrame) -> Signal | None:
            return None

        def manage(self, position: Position, bars: pd.DataFrame) -> Management:
            return self.action

    script = Script()
    bot.strategy = script
    s.klines["BTCUSDT"] = bars_rows()
    bot.executor.open(approved())
    bot.positions["BTCUSDT"] = Position("BTCUSDT", Side.LONG, 0.007, 60_000, 58_800, 58_800, NOW)
    s.klines["BTCUSDT"][-2][2:5] = ["61100", s.klines["BTCUSDT"][-2][3], "61000"]
    bot.last_reconcile = NOW
    bot.healthy = True
    bot.on_bars(NOW)
    assert s.positions["BTCUSDT"]["stopLoss"] == 59_500

    script.action = Management(exit=True, reason="sortie 10 bougies")
    bot.on_bars(NOW + BAR)
    s.klines["BTCUSDT"] = bars_rows(end=NOW + BAR)
    bot.on_bars(NOW + BAR)
    assert "BTCUSDT" not in bot.positions and "BTCUSDT" not in s.positions
    assert st.list_trades()[0]["exit_reason"] == "sortie 10 bougies"


def test_bot_vetoes_when_unhealthy(tmp_path: Path) -> None:
    session = FakeSession()
    session.orders.append({"symbol": "ETHUSDT", "side": "Buy", "orderLinkId": "manual"})
    bot, s, rec, st = make_bot(tmp_path, session)
    s.klines["BTCUSDT"] = bars_rows()
    s.marks["BTCUSDT"] = 56_400
    bot.tick(NOW)
    assert not bot.positions
    assert "reconcile" in rec.kinds()
    assert any("incertain" in e["message"] for e in st.list_events())


def test_bot_skips_bad_data(tmp_path: Path) -> None:
    bot, s, rec, _ = make_bot(tmp_path)
    rows = bars_rows()
    del rows[100]  # trou de données
    s.klines["BTCUSDT"] = rows
    bot.tick(NOW)
    assert not bot.positions and "data" in rec.kinds()

    class Down(FakeSession):
        def get_kline(self, **kw: object) -> dict[str, object]:
            raise FakeBybitError("down", 10001)

    (tmp_path / "x").mkdir()
    bot2, _, rec2, _ = make_bot(tmp_path / "x", Down())
    bot2.tick(NOW)
    assert "data" in rec2.kinds()


def test_bot_api_errors(tmp_path: Path) -> None:
    class Flaky(FakeSession):
        fail_wallet = True
        fail_tickers = False
        fail_order = False

        def get_wallet_balance(self, **kw: object) -> dict[str, object]:
            if self.fail_wallet:
                raise FakeBybitError("down", 10001)
            return super().get_wallet_balance(**kw)

        def get_tickers(self, **kw: object) -> dict[str, object]:
            if self.fail_tickers:
                raise FakeBybitError("down", 10001)
            return super().get_tickers(**kw)

        def place_order(self, **kw: object) -> dict[str, object]:
            if self.fail_order:
                raise FakeBybitError("insufficient", 110007)
            return super().place_order(**kw)

    session = Flaky()
    bot, s, rec, _ = make_bot(tmp_path, session)
    s.klines["BTCUSDT"] = bars_rows()
    bot.tick(NOW)
    assert not bot.healthy and "api" in rec.kinds()

    session.fail_wallet = False
    session.fail_tickers = True
    bot.last_bar.clear()
    bot.tick(NOW + timedelta(minutes=2))
    assert not bot.positions

    session.fail_tickers = False
    session.fail_order = True
    s.marks["BTCUSDT"] = 56_400
    bot.last_bar.clear()
    bot.tick(NOW + timedelta(minutes=4))
    assert not bot.positions and not bot.healthy

    # Ordre accepté mais position sans stop : fermée, entrées bloquées.
    session.fail_order = False
    s.drop_stop = True
    bot.last_bar.clear()
    bot.healthy = True
    bot.last_reconcile = NOW + timedelta(minutes=6)
    bot.on_bars(NOW + timedelta(minutes=6))
    assert not bot.positions and not bot.healthy


def test_bot_reconcile_actions(tmp_path: Path) -> None:
    class S(FakeSession):
        fail_close = False
        fail_stop = False

        def place_order(self, **kw: object) -> dict[str, object]:
            if kw.get("reduceOnly") and self.fail_close:
                raise FakeBybitError("x", 10001)
            return super().place_order(**kw)

        def set_trading_stop(self, **kw: object) -> dict[str, object]:
            if self.fail_stop:
                raise FakeBybitError("x", 10001)
            return super().set_trading_stop(**kw)

    session = S()
    bot, s, rec, _ = make_bot(tmp_path, session)
    s.positions["BTCUSDT"] = {
        "side": "Buy",
        "size": 0.01,
        "avgPrice": 60_000,
        "stopLoss": None,
        "liqPrice": 40_000,
    }
    session.fail_close = True
    bot.do_reconcile(NOW)
    assert not bot.healthy and "échec de clôture" in rec.messages[-1][2]
    session.fail_close = False
    bot.do_reconcile(NOW)
    assert "BTCUSDT" not in s.positions

    s.positions["BTCUSDT"] = {
        "side": "Buy",
        "size": 0.01,
        "avgPrice": 60_000,
        "stopLoss": 58_000,
        "liqPrice": 40_000,
    }
    bot.positions = {"BTCUSDT": Position("BTCUSDT", Side.LONG, 0.01, 60_000, 59_000, 58_000, NOW)}
    bot.do_reconcile(NOW)
    assert s.positions["BTCUSDT"]["stopLoss"] == 59_000 and bot.healthy
    s.positions["BTCUSDT"]["stopLoss"] = 58_000
    session.fail_stop = True
    bot.do_reconcile(NOW)
    assert not bot.healthy


def test_bot_management_api_error(tmp_path: Path) -> None:
    class S(FakeSession):
        def set_trading_stop(self, **kw: object) -> dict[str, object]:
            raise FakeBybitError("x", 10001)

    bot, s, rec, _ = make_bot(tmp_path, S())

    class Script:
        name = "trend"
        warmup = 1

        def signal(self, symbol: str, bars: pd.DataFrame) -> Signal | None:
            return None

        def manage(self, position: Position, bars: pd.DataFrame) -> Management:
            return Management(new_stop=59_500)

    bot.strategy = Script()
    s.klines["BTCUSDT"] = bars_rows()
    bot.positions["BTCUSDT"] = Position("BTCUSDT", Side.LONG, 0.007, 60_000, 58_800, 58_800, NOW)
    s.klines["BTCUSDT"][-2][2:5] = ["61100", s.klines["BTCUSDT"][-2][3], "61000"]
    bot.on_bars(NOW)
    assert not bot.healthy and rec.messages[-1][1] == "api"


def test_bot_commands_and_summary(tmp_path: Path) -> None:
    bot, s, rec, st = make_bot(tmp_path)
    s.klines["BTCUSDT"] = bars_rows(breakout=False)
    bot.tick(NOW)
    st.push_command("pause", "dashboard")
    bot.tick(NOW + timedelta(seconds=5))
    assert status(bot) is BotStatus.PAUSE
    bot.executor.open(approved())
    bot.positions["BTCUSDT"] = Position("BTCUSDT", Side.LONG, 0.007, 60_000, 58_800, 58_800, NOW)
    st.push_command("kill", "telegram")
    bot.tick(NOW + timedelta(seconds=10))
    assert status(bot) is BotStatus.HALT
    assert not s.positions and st.list_trades()[0]["exit_reason"] == "kill switch"
    bot.tick(NOW + timedelta(days=1))
    assert "summary" in rec.kinds()


def test_admin_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/a.db")
    st = Store(f"sqlite:///{tmp_path}/a.db")
    assert admin_main(["status"]) == 0
    assert admin_main(["resume"]) == 1
    assert admin_main(["clear-halt"]) == 1
    risk = RiskManager(RiskConfig())
    risk.pause()
    st.put("risk_state", risk.state.to_dict())
    assert admin_main(["resume"]) == 0
    risk.resume()
    risk.kill()
    st.put("risk_state", risk.state.to_dict())
    assert admin_main(["clear-halt"], ask=lambda _: "non") == 1
    assert admin_main(["clear-halt"], ask=lambda _: HALT_CONFIRMATION) == 0
    assert st.get("risk_state")["status"] == "PAUSE"
    assert "PAUSE" in capsys.readouterr().out


# ---------------------------------------------------------------- démarrage
def test_build_bot_startup_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "demo")
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    settings = Settings()
    st = Store(f"sqlite:///{tmp_path}/b.db")
    n = TelegramNotifier(settings, sink=st)

    import bot.main as main_mod

    s = FakeSession()
    s.key_info["expiredAt"] = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    monkeypatch.setattr(
        main_mod,
        "datetime",
        type("D", (), {"now": staticmethod(lambda tz=None: datetime.fromtimestamp(1.7e9, UTC))}),
    )
    bot = build_bot(settings, BybitClient(s, sleep=lambda _: None), st, n)
    assert bot.symbols == ["BTCUSDT", "ETHUSDT"]
    assert any("expire" in e["message"] for e in st.list_events())

    s.key_info["permissions"]["Wallet"] = ["Withdraw"]
    with pytest.raises(ForbiddenPermissionError):
        build_bot(settings, BybitClient(s, sleep=lambda _: None), st, n)

    s.key_info["permissions"]["Wallet"] = []
    monkeypatch.setattr(
        main_mod,
        "datetime",
        type("D", (), {"now": staticmethod(lambda tz=None: datetime.fromtimestamp(1.8e9, UTC))}),
    )
    with pytest.raises(ExchangeError, match="Horloge"):
        build_bot(settings, BybitClient(s, sleep=lambda _: None), st, n)


def test_decimal_qty_sent() -> None:
    s, _, _, ex, _ = setup()
    ex.open(approved())
    kw = next(kw for n, kw in s.calls if n == "place_order")
    assert Decimal(kw["qty"]) == Decimal("0.007")
