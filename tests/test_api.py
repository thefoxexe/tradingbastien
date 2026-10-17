from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.telegram import TelegramBot, check_silence
from bot.config import Settings
from bot.models import ClosedTrade, Position, Side
from bot.risk.limits import RiskConfig
from bot.store import Store

NOW = datetime.now(UTC)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(f"sqlite:///{tmp_path}/api.db")


@pytest.fixture
def client(store: Store) -> Iterator[TestClient]:
    api_main.app.dependency_overrides[api_main.get_store] = lambda: store
    api_main.get_settings.cache_clear()
    with TestClient(api_main.app) as c:
        yield c
    api_main.app.dependency_overrides.clear()


def seed(store: Store) -> None:
    store.put(
        "snapshot",
        {
            "ts": NOW.isoformat(),
            "mode": "demo",
            "status": "RUN",
            "status_reason": "",
            "pause_until": None,
            "equity": 1000.0,
            "healthy": True,
            "daily_pnl_pct": -0.01,
            "weekly_pnl_pct": 0.02,
            "drawdown": 0.05,
            "consecutive_losses": 1,
            "open_risk": 10.0,
            "limits": RiskConfig().as_table(),
        },
    )
    store.save_positions(
        {
            "BTCUSDT": Position(
                "BTCUSDT",
                Side.LONG,
                0.01,
                60_000,
                59_000,
                58_800,
                NOW,
                liq_price=40_000,
                mark_price=61_000,
            )
        }
    )
    store.add_trade(
        ClosedTrade(
            "BTCUSDT", Side.LONG, 0.01, 60_000, 61_200, 58_800, NOW, NOW, 0.7, 0.1, "trailing"
        )
    )
    store.add_trade(
        ClosedTrade("ETHUSDT", Side.SHORT, 1, 3000, 3050, 3060, NOW, NOW, 1.6, 0.0, "stop")
    )
    for i in range(5):
        store.record_equity(NOW - timedelta(hours=5 - i), 1000 + i * 10 - (30 if i == 3 else 0))
    store.log_event("entry", "Entrée long BTCUSDT")


def test_dashboard_pages(client: TestClient, store: Store) -> None:
    seed(store)
    assert "Kill switch" in client.get("/").text
    o = client.get("/api/overview").json()
    assert o["status"] == "RUN" and not o["silent"] and o["equity"] == 1000
    assert o["total_pnl"] == pytest.approx(12 - 0.8 - 50 - 1.6)
    p = client.get("/api/positions").json()[0]
    assert p["effective_leverage"] == pytest.approx(0.61) and p["risk_usdt"] == pytest.approx(10)
    assert len(client.get("/api/trades").json()) == 2
    r = client.get("/api/risk").json()
    names = [g["name"] for g in r["gauges"]]
    assert "Drawdown depuis le pic" in names and r["gauges"][0]["used"] == pytest.approx(0.01)
    perf = client.get("/api/performance").json()
    assert len(perf["equity"]) == 5 and min(d["value"] for d in perf["drawdown"]) < 0
    assert {s["strategy"] for s in perf["strategies"]} == {"trend"}
    assert client.get("/api/journal").json()[0]["kind"] == "entry"
    s = client.get("/api/settings").json()
    assert s["mode"] == "backtest" and any(row["cap"] == 0.02 for row in s["risk"])
    assert client.get("/health").json()["status"] == "ok"


def test_dashboard_empty_state(client: TestClient) -> None:
    o = client.get("/api/overview").json()
    assert o["silent"] and o["status"] == "INCONNU"
    assert client.get("/api/positions").json() == []
    assert client.get("/api/risk").json()["gauges"][3]["used"] == 0
    assert client.get("/api/performance").json()["strategies"] == []


def test_no_secret_exposed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", "super-secret-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "super-secret-value")
    api_main.get_settings.cache_clear()
    body = "".join(client.get(p).text for p in ("/api/settings", "/api/overview", "/"))
    assert "super-secret" not in body


def test_actions_require_header_and_confirmation(client: TestClient, store: Store) -> None:
    assert client.post("/api/pause").status_code == 403
    assert client.post("/api/kill", json={"confirm": "KILL"}).status_code == 403
    h = {"X-Requested-With": "dashboard"}
    assert client.post("/api/pause", headers=h).status_code == 200
    assert client.post("/api/kill", headers=h, json={"confirm": "oui"}).status_code == 400
    assert client.post("/api/kill", headers=h, json={"confirm": "KILL"}).status_code == 200
    assert store.pop_commands() == [("kill", "dashboard"), ("pause", "dashboard")]


def test_no_route_to_raise_risk_or_resume(client: TestClient) -> None:
    paths = {getattr(r, "path", "") for r in api_main.app.routes}
    posts = {
        getattr(r, "path", "")
        for r in api_main.app.routes
        if "POST" in getattr(r, "methods", set())
    }
    assert posts == {"/api/pause", "/api/kill"}
    assert not any("resume" in p or "halt" in p for p in paths)


# ---------------------------------------------------------------- Telegram
def make_tg(
    store: Store, monkeypatch: pytest.MonkeyPatch, updates: list[dict[str, object]]
) -> tuple[TelegramBot, list[str], list[datetime]]:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"result": updates})
        sent.append(req.content.decode())
        return httpx.Response(200 if len(sent) < 99 else 500)

    clock = [NOW]
    tg = TelegramBot(
        Settings(),
        store,
        httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: clock[0],
    )
    return tg, sent, clock


def test_telegram_commands(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(store)

    def msg(text: str, chat: int = 42) -> dict[str, object]:
        return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}

    tg, sent, clock = make_tg(store, monkeypatch, [msg("/status"), msg("/kill", chat=666)])
    tg.poll_once()
    assert len(sent) == 1 and "RUN" in sent[0]  # le chat inconnu est ignoré
    assert "Confirmez" in tg.handle("/kill")
    clock[0] += timedelta(seconds=90)
    assert "Aucune demande" in tg.handle("/kill CONFIRMER")  # délai dépassé
    tg.handle("/kill")
    assert "KILL SWITCH" in tg.handle("/kill@monbot confirmer")
    assert "Pause" in tg.handle("/pause")
    assert "Commandes" in tg.handle("bonjour")
    assert store.pop_commands() == [("kill", "telegram"), ("pause", "telegram")]


def test_telegram_send_error_logged(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    tg = TelegramBot(
        Settings(),
        store,
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    tg.send("x")  # pas d'exception


def test_silence_detection(store: Store) -> None:
    assert check_silence(store, NOW)
    seed(store)
    assert not check_silence(store, NOW + timedelta(minutes=4))
    assert check_silence(store, NOW + timedelta(minutes=6))
