"""Point d'entrée du service ``bot``.

Démarrage : configuration validée → permissions de la clé contrôlées →
règles d'instruments → marge isolée / one-way / levier → réconciliation →
boucle. Toute anomalie au démarrage = refus de démarrer.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from bot.alerts import TelegramNotifier
from bot.config import Mode, Settings
from bot.exchange.client import BybitClient, ExchangeError, make_session
from bot.exchange.permissions import ForbiddenPermissionError, check_api_key
from bot.exchange.stream import MarketStream
from bot.execution.executor import Executor
from bot.logs import setup_logging
from bot.risk.manager import RiskManager
from bot.runner import Bot, http_heartbeat
from bot.store import Store
from bot.strategy.trend import TrendFollowing

log = logging.getLogger("bot")
MAX_CLOCK_SKEW = timedelta(seconds=2)


def build_bot(
    settings: Settings, client: BybitClient, store: Store, notifier: TelegramNotifier
) -> Bot:
    """Contrôles de démarrage puis assemblage. Lève en cas de refus."""
    key = check_api_key(client.api_key_info(), require_ip=settings.mode is Mode.LIVE)
    for warning in key.warnings:
        notifier.send(warning, level="WARNING", kind="api_key")

    skew = abs(client.server_time() - datetime.now(UTC))
    if skew > MAX_CLOCK_SKEW:
        raise ExchangeError(
            f"Horloge décalée de {skew.total_seconds():.1f} s : vérifier chrony/NTP"
        )

    cfg = settings.risk_config()
    rules = client.instruments(settings.symbols)
    for sym in settings.symbols:
        client.setup_symbol(sym, cfg.exchange_leverage)

    risk = RiskManager(cfg, max_capital=settings.max_capital_usdt)
    executor = Executor(client, risk, rules, notifier, fee_taker=cfg.fee_taker)
    return Bot(
        client=client,
        store=store,
        notifier=notifier,
        risk=risk,
        executor=executor,
        strategy=TrendFollowing(),
        rules=rules,
        symbols=settings.symbols,
        mode=settings.mode.value,
        reconcile_every=timedelta(seconds=settings.reconcile_interval_s),
        heartbeat=http_heartbeat(settings.heartbeat_url) if settings.heartbeat_url else None,
    )


def main() -> int:  # pragma: no cover - assemblage réseau, testé par morceaux
    try:
        settings = Settings()
    except (ValidationError, ValueError) as exc:
        # Les SecretStr restent masqués dans le message de validation.
        print(f"Configuration invalide, démarrage refusé : {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    if settings.mode is Mode.BACKTEST:
        print("Mode backtest : utiliser `python -m bot.backtest download` puis `run`.")
        return 0

    store = Store(settings.database_url.get_secret_value())
    notifier = TelegramNotifier(settings, sink=store)
    try:
        bot = build_bot(settings, BybitClient(make_session(settings)), store, notifier)
    except (ForbiddenPermissionError, ExchangeError) as exc:
        notifier.send(f"Démarrage refusé : {exc}", level="CRITICAL", kind="startup")
        return 3

    stream = MarketStream(settings.symbols)
    stream.start()
    bot.stream = stream
    notifier.send(
        f"Bot démarré en mode {settings.mode.value.upper()} sur {', '.join(settings.symbols)}",
        kind="startup",
    )
    try:
        bot.run_forever(settings.loop_interval_s)
    finally:
        stream.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
