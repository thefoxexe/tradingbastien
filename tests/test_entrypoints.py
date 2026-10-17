from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from bot.main import main


def test_bot_starts_in_backtest() -> None:
    assert main() == 0


def test_bot_refuses_invalid_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODE", "live")
    assert main() == 2
    assert "démarrage refusé" in capsys.readouterr().err


def test_api_health() -> None:
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
