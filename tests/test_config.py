from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from bot.config import BYBIT_REST_DEMO, BYBIT_REST_LIVE, Mode, Settings

ROOT = Path(__file__).resolve().parent.parent
FAKE_KEY = "fake-key-123"
FAKE_SECRET = "fake-secret-456"


def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", FAKE_KEY)
    monkeypatch.setenv("BYBIT_API_SECRET", FAKE_SECRET)


def test_default_is_backtest_without_url() -> None:
    s = Settings()
    assert s.mode is Mode.BACKTEST
    assert s.rest_base_url is None
    assert s.symbols == ["BTCUSDT", "ETHUSDT"]


def test_demo_uses_demo_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "demo")
    _keys(monkeypatch)
    assert Settings().rest_base_url == BYBIT_REST_DEMO


def test_demo_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "demo")
    with pytest.raises(ValidationError, match="BYBIT_API_KEY"):
        Settings()


@pytest.mark.parametrize(
    ("confirmed", "capital", "message"),
    [
        (None, "1000", "LIVE_CONFIRMED"),
        ("no", "1000", "LIVE_CONFIRMED"),
        ("YES", "1000", "LIVE_CONFIRMED"),
        ("yes", None, "MAX_CAPITAL_USDT"),
    ],
)
def test_live_refused_without_confirmation_and_capital(
    monkeypatch: pytest.MonkeyPatch, confirmed: str | None, capital: str | None, message: str
) -> None:
    monkeypatch.setenv("MODE", "live")
    _keys(monkeypatch)
    if confirmed is not None:
        monkeypatch.setenv("LIVE_CONFIRMED", confirmed)
    if capital is not None:
        monkeypatch.setenv("MAX_CAPITAL_USDT", capital)
    with pytest.raises(ValidationError, match=message):
        Settings()


def test_live_rejects_non_positive_capital(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("LIVE_CONFIRMED", "yes")
    monkeypatch.setenv("MAX_CAPITAL_USDT", "0")
    _keys(monkeypatch)
    with pytest.raises(ValidationError):
        Settings()


def test_live_accepted_with_confirmation_and_capital(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("LIVE_CONFIRMED", "yes")
    monkeypatch.setenv("MAX_CAPITAL_USDT", "1000")
    _keys(monkeypatch)
    s = Settings()
    assert s.rest_base_url == BYBIT_REST_LIVE
    assert s.max_capital_usdt == 1000


def test_symbols_outside_v1_scope_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYMBOLS", '["BTCUSDT","PEPEUSDT"]')
    with pytest.raises(ValidationError, match="PEPEUSDT"):
        Settings()


def test_secrets_never_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "demo")
    _keys(monkeypatch)
    s = Settings()
    for text in (repr(s), str(s), str(s.model_dump())):
        assert FAKE_KEY not in text
        assert FAKE_SECRET not in text


def test_secrets_not_leaked_by_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "live")
    _keys(monkeypatch)
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert FAKE_SECRET not in str(exc.value)
    assert FAKE_KEY not in str(exc.value)


def test_env_example_loads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Le .env.example fourni doit être chargeable tel quel (mode backtest)."""
    (tmp_path / ".env").write_text((ROOT / ".env.example").read_text())
    monkeypatch.chdir(tmp_path)
    assert Settings().mode is Mode.BACKTEST
