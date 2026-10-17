"""Persistance : PostgreSQL en production, SQLite en test (SQLAlchemy Core).

La base complète l'état reconstruit depuis Bybit (qui fait toujours foi) :
stops initiaux, historique des trades, journal, commandes du dashboard.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from bot.models import ClosedTrade, Position, Side

metadata = MetaData()

kv = Table(
    "kv",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
trades = Table(
    "trades",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(20), nullable=False),
    Column("side", String(5), nullable=False),
    Column("qty", Float, nullable=False),
    Column("entry", Float, nullable=False),
    Column("exit", Float, nullable=False),
    Column("initial_stop", Float, nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=False),
    Column("fees", Float, nullable=False),
    Column("funding", Float, nullable=False),
    Column("net_pnl", Float, nullable=False),
    Column("r_multiple", Float, nullable=False),
    Column("exit_reason", String(200), nullable=False),
    Column("strategy", String(40), nullable=False),
)
events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False, index=True),
    Column("level", String(10), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("message", Text, nullable=False),
)
equity = Table(
    "equity",
    metadata,
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("equity", Float, nullable=False),
)
commands = Table(
    "commands",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("command", String(20), nullable=False),
    Column("source", String(40), nullable=False),
    Column("processed_at", DateTime(timezone=True), nullable=True),
)

ALLOWED_COMMANDS = frozenset({"pause", "kill"})


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _position_to_dict(p: Position) -> dict[str, Any]:
    return {
        "symbol": p.symbol,
        "side": p.side.value,
        "qty": p.qty,
        "entry": p.entry,
        "stop": p.stop,
        "initial_stop": p.initial_stop,
        "opened_at": p.opened_at.isoformat(),
        "strategy": p.strategy,
        "liq_price": p.liq_price,
        "mark_price": p.mark_price,
        "unrealized_pnl": p.unrealized_pnl,
        "fees": p.fees,
        "funding": p.funding,
    }


def _position_from_dict(d: Mapping[str, Any]) -> Position:
    return Position(
        symbol=d["symbol"],
        side=Side(d["side"]),
        qty=float(d["qty"]),
        entry=float(d["entry"]),
        stop=float(d["stop"]),
        initial_stop=float(d["initial_stop"]),
        opened_at=datetime.fromisoformat(d["opened_at"]),
        strategy=d.get("strategy", "trend"),
        liq_price=d.get("liq_price"),
        mark_price=d.get("mark_price"),
        unrealized_pnl=float(d.get("unrealized_pnl") or 0),
        fees=float(d.get("fees") or 0),
        funding=float(d.get("funding") or 0),
    )


class Store:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True, future=True)
        metadata.create_all(self.engine)

    # ------------------------------------------------------------------ clé/valeur
    def put(self, key: str, value: Any) -> None:
        now = datetime.now(UTC)
        payload = json.dumps(value, default=str)
        with self.engine.begin() as c:
            found = c.execute(select(kv.c.key).where(kv.c.key == key)).first()
            if found:
                c.execute(update(kv).where(kv.c.key == key).values(value=payload, updated_at=now))
            else:
                c.execute(insert(kv).values(key=key, value=payload, updated_at=now))

    def get(self, key: str, default: Any = None) -> Any:
        with self.engine.connect() as c:
            row = c.execute(select(kv.c.value).where(kv.c.key == key)).first()
        return json.loads(row[0]) if row else default

    def updated_at(self, key: str) -> datetime | None:
        with self.engine.connect() as c:
            row = c.execute(select(kv.c.updated_at).where(kv.c.key == key)).first()
        return _utc(row[0]) if row else None

    # ------------------------------------------------------------------ positions locales
    def save_positions(self, positions: Mapping[str, Position]) -> None:
        self.put("positions", [_position_to_dict(p) for p in positions.values()])

    def load_positions(self) -> dict[str, Position]:
        return {d["symbol"]: _position_from_dict(d) for d in self.get("positions", [])}

    # ------------------------------------------------------------------ trades
    def add_trade(self, t: ClosedTrade) -> None:
        with self.engine.begin() as c:
            c.execute(
                insert(trades).values(
                    symbol=t.symbol,
                    side=t.side.value,
                    qty=t.qty,
                    entry=t.entry,
                    exit=t.exit,
                    initial_stop=t.initial_stop,
                    opened_at=t.opened_at,
                    closed_at=t.closed_at,
                    fees=t.fees,
                    funding=t.funding,
                    net_pnl=t.net_pnl,
                    r_multiple=t.r_multiple,
                    exit_reason=t.exit_reason,
                    strategy=t.strategy,
                )
            )

    def list_trades(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(trades).order_by(trades.c.closed_at.desc()).limit(limit)
            ).mappings()
            return [
                {**r, "opened_at": _utc(r["opened_at"]), "closed_at": _utc(r["closed_at"])}
                for r in rows
            ]

    # ------------------------------------------------------------------ journal
    def log_event(self, kind: str, message: str, level: str = "INFO") -> None:
        with self.engine.begin() as c:
            c.execute(
                insert(events).values(ts=datetime.now(UTC), level=level, kind=kind, message=message)
            )

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(select(events).order_by(events.c.id.desc()).limit(limit)).mappings()
            return [{**r, "ts": _utc(r["ts"])} for r in rows]

    # ------------------------------------------------------------------ equity
    def record_equity(self, ts: datetime, value: float) -> None:
        with self.engine.begin() as c:
            c.execute(delete(equity).where(equity.c.ts == ts))
            c.execute(insert(equity).values(ts=ts, equity=value))

    def equity_curve(self, limit: int = 5000) -> list[tuple[datetime, float]]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(equity.c.ts, equity.c.equity).order_by(equity.c.ts.desc()).limit(limit)
            ).all()
        return [(_utc(ts) or ts, float(v)) for ts, v in reversed(rows)]

    # ------------------------------------------------------------------ commandes (dashboard, Telegram)
    def push_command(self, command: str, source: str) -> int:
        if command not in ALLOWED_COMMANDS:
            raise ValueError(f"Commande refusée : {command}")
        with self.engine.begin() as c:
            res = c.execute(
                insert(commands).values(ts=datetime.now(UTC), command=command, source=source)
            )
            return int(res.inserted_primary_key[0]) if res.inserted_primary_key else 0

    def pop_commands(self) -> list[tuple[str, str]]:
        """Commandes non traitées, marquées comme traitées (kill d'abord)."""
        with self.engine.begin() as c:
            rows = c.execute(
                select(commands.c.id, commands.c.command, commands.c.source)
                .where(commands.c.processed_at.is_(None))
                .order_by(commands.c.id)
            ).all()
            if rows:
                c.execute(
                    update(commands)
                    .where(commands.c.id.in_([r[0] for r in rows]))
                    .values(processed_at=datetime.now(UTC))
                )
        out = [(str(r[1]), str(r[2])) for r in rows]
        return sorted(out, key=lambda x: x[0] != "kill")
