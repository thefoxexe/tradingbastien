"""Logs JSON sur stdout (la rotation est confiée à Docker).

Aucune clé ni secret ne doit apparaître dans les logs : les champs dont le
nom évoque un secret sont masqués par sécurité.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_SENSITIVE = ("key", "secret", "token", "password", "signature")


def _redact(name: str, value: Any) -> Any:
    if any(s in name.lower() for s in _SENSITIVE):
        return "***"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name, value in vars(record).items():
            if name not in _RESERVED:
                payload[name] = _redact(name, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
