"""Service ``api`` : dashboard privé (joignable uniquement par Tailscale).

Lecture seule, sauf deux actions : pause et kill switch. Aucun réglage de
risque ne se modifie ici : l'API écrit seulement une commande en base, que
le bot exécute. Les clés ne sont jamais lues ni affichées.
"""

from __future__ import annotations

import math
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.telegram import TelegramBot, watchdog
from bot import __version__
from bot.backtest.metrics import max_drawdown
from bot.config import Settings
from bot.risk import limits
from bot.store import Store

STATIC = Path(__file__).parent / "static"
SILENT_AFTER = timedelta(minutes=5)
KILL_WORD = "KILL"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store(get_settings().database_url.get_secret_value())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    stop = threading.Event()
    if (
        settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id
    ):  # pragma: no cover
        tg = TelegramBot(settings, get_store())
        threading.Thread(target=tg.poll_forever, args=(stop,), daemon=True).start()
        threading.Thread(target=watchdog, args=(tg, get_store(), stop), daemon=True).start()
    yield
    stop.set()


app = FastAPI(
    title="tradingbastien", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def require_ajax(x_requested_with: str = Header(default="")) -> None:
    """Bloque les POST cross-site (formulaire d'une autre page) : seul le dashboard envoie cet en-tête."""
    if x_requested_with != "dashboard":
        raise HTTPException(403, "En-tête X-Requested-With manquant")


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
def overview(store: Store = Depends(get_store)) -> dict[str, Any]:
    snap = store.get("snapshot") or {}
    trades = store.list_trades(5000)
    total = sum(t["net_pnl"] for t in trades)
    ts = datetime.fromisoformat(snap["ts"]) if snap.get("ts") else None
    age = (datetime.now(UTC) - ts).total_seconds() if ts else None
    return {
        "mode": snap.get("mode", get_settings().mode.value),
        "status": snap.get("status", "INCONNU"),
        "status_reason": snap.get("status_reason", ""),
        "pause_until": snap.get("pause_until"),
        "equity": snap.get("equity"),
        "healthy": snap.get("healthy", False),
        "daily_pnl_pct": snap.get("daily_pnl_pct", 0.0),
        "weekly_pnl_pct": snap.get("weekly_pnl_pct", 0.0),
        "total_pnl": total,
        "drawdown": snap.get("drawdown", 0.0),
        "max_drawdown": limits.MAX_DRAWDOWN,
        "last_update": snap.get("ts"),
        "silent": age is None or age > SILENT_AFTER.total_seconds(),
        "version": __version__,
    }


@app.get("/api/positions")
def positions(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    equity = (store.get("snapshot") or {}).get("equity") or 0.0
    out = []
    for p in store.load_positions().values():
        price = p.mark_price or p.entry
        out.append(
            {
                "symbol": p.symbol,
                "side": p.side.value,
                "qty": p.qty,
                "entry": p.entry,
                "stop": p.stop,
                "initial_stop": p.initial_stop,
                "liq_price": p.liq_price,
                "mark_price": p.mark_price,
                "effective_leverage": p.qty * price / equity if equity else None,
                "unrealized_pnl": p.unrealized_pnl,
                "r_now": p.r_multiple(price),
                "risk_usdt": p.open_risk,
                "opened_at": p.opened_at.isoformat(),
            }
        )
    return out


@app.get("/api/trades")
def trades(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return [
        {**t, "opened_at": t["opened_at"].isoformat(), "closed_at": t["closed_at"].isoformat()}
        for t in store.list_trades(1000)
    ]


@app.get("/api/risk")
def risk(store: Store = Depends(get_store)) -> dict[str, Any]:
    snap = store.get("snapshot") or {}
    equity = snap.get("equity") or 0.0
    rows = {r["name"]: r["value"] for r in snap.get("limits", [])}
    total_limit = rows.get("max_total_open_risk", limits.MAX_TOTAL_OPEN_RISK)

    def gauge(name: str, used: float, limit: float, unit: str = "%") -> dict[str, Any]:
        return {
            "name": name,
            "used": used,
            "limit": limit,
            "ratio": used / limit if limit else 0.0,
            "unit": unit,
        }

    return {
        "gauges": [
            gauge(
                "Perte du jour",
                max(0.0, -snap.get("daily_pnl_pct", 0.0)),
                rows.get("max_daily_loss", limits.MAX_DAILY_LOSS),
            ),
            gauge(
                "Perte de la semaine",
                max(0.0, -snap.get("weekly_pnl_pct", 0.0)),
                rows.get("max_weekly_loss", limits.MAX_WEEKLY_LOSS),
            ),
            gauge(
                "Drawdown depuis le pic",
                snap.get("drawdown", 0.0),
                rows.get("max_drawdown", limits.MAX_DRAWDOWN),
            ),
            gauge(
                "Risque ouvert",
                (snap.get("open_risk", 0.0) / equity) if equity else 0.0,
                total_limit,
            ),
            gauge(
                "Pertes consécutives",
                float(snap.get("consecutive_losses", 0)),
                float(rows.get("max_consecutive_losses", limits.MAX_CONSECUTIVE_LOSSES)),
                unit="",
            ),
        ],
        "status": snap.get("status", "INCONNU"),
        "status_reason": snap.get("status_reason", ""),
    }


@app.get("/api/performance")
def performance(store: Store = Depends(get_store)) -> dict[str, Any]:
    # Lightweight Charts exige des temps strictement croissants : un point par seconde.
    points = sorted({int(ts.timestamp()): v for ts, v in store.equity_curve()}.items())
    peak = -math.inf
    dd = []
    for when, v in points:
        peak = max(peak, v)
        dd.append({"time": when, "value": (v / peak - 1) * 100 if peak > 0 else 0.0})
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for t in store.list_trades(5000):
        by_strategy.setdefault(t["strategy"], []).append(t)
    stats = []
    for name, ts_ in sorted(by_strategy.items()):
        gains = sum(t["net_pnl"] for t in ts_ if t["net_pnl"] > 0)
        losses = -sum(t["net_pnl"] for t in ts_ if t["net_pnl"] < 0)
        stats.append(
            {
                "strategy": name,
                "trades": len(ts_),
                "win_rate": sum(t["net_pnl"] > 0 for t in ts_) / len(ts_),
                "profit_factor": _finite(gains / losses) if losses else None,
                "avg_r": sum(t["r_multiple"] for t in ts_) / len(ts_),
                "net_pnl": sum(t["net_pnl"] for t in ts_),
                "fees": sum(t["fees"] for t in ts_),
                "funding": sum(t["funding"] for t in ts_),
            }
        )
    return {
        "equity": [{"time": when, "value": v} for when, v in points],
        "drawdown": dd,
        "max_drawdown": max_drawdown([v for _, v in points]),
        "strategies": stats,
    }


@app.get("/api/journal")
def journal(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return [{**e, "ts": e["ts"].isoformat()} for e in store.list_events(300)]


@app.get("/api/settings")
def settings_view(store: Store = Depends(get_store)) -> dict[str, Any]:
    s = get_settings()
    cfg = s.risk_config()
    return {
        "mode": s.mode.value,
        "symbols": s.symbols,
        "max_capital_usdt": s.max_capital_usdt,
        "risk": cfg.as_table(),
        "liq_distance_multiple_min": limits.MIN_LIQ_DISTANCE_MULTIPLE,
        "strategy": "Tendance 4 h : EMA 50/200, cassure 20 bougies, stop 2 ATR, break-even +1R, trailing 3 ATR",
        "telegram": bool(s.telegram_chat_id),
    }


class KillRequest(BaseModel):
    confirm: str


@app.post("/api/pause", dependencies=[Depends(require_ajax)])
def pause(store: Store = Depends(get_store)) -> dict[str, str]:
    store.push_command("pause", "dashboard")
    store.log_event("dashboard", "Pause demandée depuis le dashboard", "WARNING")
    return {"status": "pause demandée"}


@app.post("/api/kill", dependencies=[Depends(require_ajax)])
def kill(req: KillRequest, store: Store = Depends(get_store)) -> dict[str, str]:
    if req.confirm != KILL_WORD:
        raise HTTPException(400, f"Tapez {KILL_WORD} pour confirmer")
    store.push_command("kill", "dashboard")
    store.log_event("dashboard", "KILL SWITCH demandé depuis le dashboard", "CRITICAL")
    return {"status": "kill switch demandé"}
