"""Bougies et funding Bybit : téléchargement, stockage CSV et contrôles de qualité."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd

INTERVAL = "240"
BAR = timedelta(hours=4)
COLUMNS = ["open", "high", "low", "close", "volume"]


class MarketSource(Protocol):
    def klines(
        self, symbol: str, interval: str, start_ms: int | None = ..., end_ms: int | None = ..., limit: int = ...
    ) -> list[list[str]]: ...

    def funding_history(
        self, symbol: str, start_ms: int | None = ..., end_ms: int | None = ...
    ) -> list[dict[str, str]]: ...


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def rows_to_frame(rows: list[list[str]]) -> pd.DataFrame:
    """Lignes Bybit ``[start, open, high, low, close, volume, turnover]`` → DataFrame indexé UTC."""
    if not rows:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz=UTC, name="time"), dtype=float)
    df = pd.DataFrame([r[:6] for r in rows], columns=["time", *COLUMNS])
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("time").astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def download_klines(
    source: MarketSource, symbol: str, start: datetime, end: datetime, interval: str = INTERVAL
) -> pd.DataFrame:
    rows: list[list[str]] = []
    cursor = _ms(start)
    end_ms = _ms(end)
    while cursor <= end_ms:
        batch = source.klines(symbol, interval, start_ms=cursor, end_ms=end_ms, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        last = int(batch[-1][0])
        if last < cursor:
            break  # pragma: no cover - défense contre une pagination incohérente
        cursor = last + 1
    return rows_to_frame(rows)


def download_funding(source: MarketSource, symbol: str, start: datetime, end: datetime) -> pd.Series:
    """Historique de funding (taux par période), paginé à rebours par blocs de 200."""
    out: dict[int, float] = {}
    end_ms, start_ms = _ms(end), _ms(start)
    while end_ms > start_ms:
        batch = source.funding_history(symbol, start_ms=start_ms, end_ms=end_ms)
        if not batch:
            break
        for r in batch:
            out[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
        first = int(batch[0]["fundingRateTimestamp"])
        if first >= end_ms:
            break  # pragma: no cover
        end_ms = first - 1
    s = pd.Series(out, dtype=float).sort_index()
    s.index = pd.to_datetime(s.index.astype("int64"), unit="ms", utc=True)
    s.index.name = "time"
    return s.rename("rate")


def closed_bars(df: pd.DataFrame, now: datetime, bar: timedelta = BAR) -> pd.DataFrame:
    """Retire la bougie en cours : un signal ne se calcule que sur bougie close."""
    if df.empty:
        return df
    last_open = df.index[-1].to_pydatetime()
    return df.iloc[:-1] if last_open + bar > now else df


def find_gaps(df: pd.DataFrame, bar: timedelta = BAR) -> list[pd.Timestamp]:
    """Ouvertures de bougies manquantes."""
    if len(df) < 2:  # noqa: PLR2004
        return []
    expected = pd.date_range(df.index[0], df.index[-1], freq=bar)
    return list(expected.difference(pd.DatetimeIndex(df.index)))


def is_valid(df: pd.DataFrame) -> bool:
    """Bougies cohérentes : pas de NaN, high ≥ max(open, close), low ≤ min(open, close), prix > 0."""
    if df.empty or df[COLUMNS[:4]].isna().any().any():
        return False
    body_hi = df[["open", "close"]].max(axis=1)
    body_lo = df[["open", "close"]].min(axis=1)
    return bool(((df["high"] >= body_hi) & (df["low"] <= body_lo) & (df["low"] > 0)).all())


def save_csv(obj: pd.DataFrame | pd.Series, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    obj.to_csv(path)


def load_candles(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col="time", parse_dates=["time"])
    df.index = pd.to_datetime(df.index, utc=True)
    return df[COLUMNS].astype(float)


def load_funding(path: Path) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype=float, name="rate")
    df = pd.read_csv(path, index_col="time", parse_dates=["time"])
    df.index = pd.to_datetime(df.index, utc=True)
    return df["rate"].astype(float)
