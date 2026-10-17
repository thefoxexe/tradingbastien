from __future__ import annotations

from pathlib import Path

import pytest

_ENV_VARS = (
    "MODE",
    "LIVE_CONFIRMED",
    "MAX_CAPITAL_USDT",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "SYMBOLS",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Chaque test part d'un environnement vide, sans lire un .env local."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
