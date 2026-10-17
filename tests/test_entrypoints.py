from __future__ import annotations

import pytest

from bot.main import main


def test_bot_backtest_mode_points_to_cli() -> None:
    assert main() == 0


def test_bot_refuses_invalid_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODE", "live")
    assert main() == 2
    assert "démarrage refusé" in capsys.readouterr().err
