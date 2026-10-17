# tradingbastien : bot de trading Bybit

Ce bot trade les futures perpétuels USDT (BTCUSDT et ETHUSDT) sur Bybit avec une stratégie de suivi de tendance
sur bougies 4 h. Il fait passer la survie du capital avant le rendement.

- La spécification fait foi : [docs/SPEC.md](docs/SPEC.md).
- Le déploiement est décrit dans [docs/DEPLOY.md](docs/DEPLOY.md).

## Règles codées en dur

- Aucun ordre sans stop-loss : le stop part dans le même ordre que l'entrée. Si aucun stop n'est visible
  chez Bybit après 5 s, la position est clôturée.
- Chaque trade risque 1 % (plafond 2 %). Le levier effectif est de 2x (plafond 3x).
  Le prix de liquidation doit être au moins 3 fois plus loin que le stop.
- Le module de risque a un droit de veto. La configuration peut abaisser les plafonds, jamais les relever.
- Les limites de perte sont : -3 % par jour, -6 % par semaine, -15 % de drawdown (HALT) et 4 pertes consécutives.
- Aucun code de retrait ou de transfert. Une clé API avec trop de droits empêche le démarrage.
- En cas de doute (erreur API, écart avec Bybit, trou de données, flux muet), le bot n'ouvre rien et alerte.

## Démarrage rapide

```bash
uv sync
cp .env.example .env                        # MODE=backtest par défaut

# Backtest sur 3 ans de données Bybit (endpoints publics, aucune clé nécessaire)
uv run python -m bot.backtest download --years 3
uv run python -m bot.backtest run           # → reports/backtest.html + verdict go/no-go

# Démo (clés du compte Demo Trading de Bybit dans .env, MODE=demo)
docker compose up -d --build                # bot + dashboard + PostgreSQL
```

## Architecture

```text
bot/
  exchange/   client Bybit V5 (pybit), permissions de la clé, règles d'instruments, flux WebSocket
  data/       bougies 4 h, funding, contrôles (trous, bougie en cours)
  strategy/   stratégies (plugins) : un signal = paire, sens, entrée, stop
  risk/       plafonds, sizing, limites, pause / HALT : seul passage vers l'exécution
  execution/  ordres avec stop, gestion des stops, kill switch, réconciliation
  backtest/   moteur (frais, slippage, funding), walk-forward, Monte Carlo, rapport HTML
  runner.py   boucle principale ; main.py : contrôles de démarrage
  admin.py    commandes manuelles : status, resume, clear-halt
api/          dashboard FastAPI (lecture seule + pause + kill switch), Telegram
```

Le même code de stratégie et de risque sert au backtest, en démo et en live.

## Développement

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest --cov
```

Ce projet n'est ni un conseil fiscal ni un conseil financier.
