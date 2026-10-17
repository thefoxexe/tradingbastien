"""Configuration chargée depuis l'environnement (.env).

Le mode (backtest, démo, live) se choisit par variable d'environnement.
Le live refuse de démarrer sans ``LIVE_CONFIRMED=yes`` et un capital maximal
déclaré (SPEC, « Architecture technique »).

Les clés API sont des ``SecretStr`` : elles ne s'affichent jamais dans un
``repr``, un log ou une exception.
"""

from __future__ import annotations

from enum import StrEnum
from functools import cached_property

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BYBIT_REST_LIVE = "https://api.bybit.com"
BYBIT_REST_DEMO = "https://api-demo.bybit.com"

# Périmètre v1 : futures perpétuels linéaires USDT, paires très liquides.
ALLOWED_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT"})


class Mode(StrEnum):
    BACKTEST = "backtest"
    DEMO = "demo"
    LIVE = "live"


class ConfigError(ValueError):
    """Configuration invalide : le bot ne démarre pas."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
        # Sinon les erreurs de validation recopient les entrées brutes, clés comprises.
        hide_input_in_errors=True,
    )

    mode: Mode = Mode.BACKTEST
    live_confirmed: str = ""
    max_capital_usdt: float | None = Field(default=None, gt=0)

    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    recv_window_ms: int = Field(default=5000, gt=0, le=10000)

    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])

    database_url: SecretStr = SecretStr("postgresql://bot:bot@db:5432/bot")

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check(self) -> Settings:
        unknown = set(self.symbols) - ALLOWED_SYMBOLS
        if unknown:
            raise ConfigError(f"Paires hors périmètre v1 : {sorted(unknown)}")
        if self.mode is Mode.LIVE:
            if self.live_confirmed != "yes":
                raise ConfigError("Mode live refusé : LIVE_CONFIRMED=yes requis.")
            if self.max_capital_usdt is None:
                raise ConfigError("Mode live refusé : MAX_CAPITAL_USDT requis.")
        if self.mode in (Mode.DEMO, Mode.LIVE) and not (
            self.bybit_api_key.get_secret_value() and self.bybit_api_secret.get_secret_value()
        ):
            raise ConfigError(f"Mode {self.mode} : BYBIT_API_KEY et BYBIT_API_SECRET requis.")
        return self

    @cached_property
    def rest_base_url(self) -> str | None:
        """URL REST Bybit du mode courant ; ``None`` en backtest (aucun ordre réel)."""
        if self.mode is Mode.LIVE:
            return BYBIT_REST_LIVE
        if self.mode is Mode.DEMO:
            return BYBIT_REST_DEMO
        return None
