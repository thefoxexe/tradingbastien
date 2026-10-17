# Règles du projet
- La spécification fait foi : docs/SPEC.md (ce cahier des charges).
- Ne jamais relever un plafond de risque ni contourner le module de risque.
- Aucun ordre sans stop-loss attaché. Aucun code de retrait ou de transfert.
- Ne jamais lire, afficher ni écrire de vraie clé API. Travailler en démo uniquement.
- Une étape à la fois ; tests, lint et typage au vert avant de passer à la suivante.
- Toute règle ambiguë : demander avant de coder.

# Commandes
- Installer : `uv sync`
- Lint : `uv run ruff check . && uv run ruff format --check .`
- Typage : `uv run mypy`
- Tests : `uv run pytest --cov`
- Backtest : `uv run python -m bot.backtest download --years 3 && uv run python -m bot.backtest run`

# Avancement (plan de développement, docs/SPEC.md)
- [x] 1. Squelette du repo
- [x] 2. Client Bybit (testé sur faux Bybit ; à valider en démo réelle)
- [x] 3. Module de risque (100 % de couverture, Hypothesis)
- [x] 4. Données et backtester
- [x] 5. Stratégie de base et rapport (backtest sur vraies données Bybit à lancer)
- [x] 6. Exécution et réconciliation (testées sur faux Bybit ; à valider en démo réelle)
- [x] 7. Dashboard et Telegram
- [~] 8. Déploiement VPS et monitoring : code et docs/DEPLOY.md prêts, VPS à installer à la main
- [ ] 9. Six semaines de démo, puis revue go/no-go
- [ ] 10. Live réduit, sur décision manuelle
