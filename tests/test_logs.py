from __future__ import annotations

import json
import logging
import sys

from bot.logs import JsonFormatter


def _format(record: logging.LogRecord) -> dict[str, object]:
    return json.loads(JsonFormatter().format(record))  # type: ignore[no-any-return]


def test_json_output_with_extra_fields() -> None:
    record = logging.makeLogRecord(
        {
            "name": "bot",
            "levelname": "INFO",
            "msg": "ordre %s",
            "args": ("ok",),
            "symbol": "BTCUSDT",
        }
    )
    out = _format(record)
    assert out["msg"] == "ordre ok"
    assert out["symbol"] == "BTCUSDT"
    assert out["level"] == "INFO"


def test_sensitive_fields_redacted() -> None:
    record = logging.makeLogRecord(
        {"msg": "x", "api_key": "abc", "api_secret": "def", "telegram_token": "ghi"}
    )
    out = _format(record)
    assert out["api_key"] == out["api_secret"] == out["telegram_token"] == "***"


def test_exception_included() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.makeLogRecord({"msg": "err", "exc_info": sys.exc_info()})
    assert "boom" in str(_format(record)["exc"])
