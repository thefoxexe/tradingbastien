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

# Avancement (plan de développement, docs/SPEC.md)
- [x] 1. Squelette du repo
- [ ] 2. Client Bybit
- [ ] 3. Module de risque
- [ ] 4. Données et backtester
- [ ] 5. Stratégie de base et rapport
- [ ] 6. Exécution et réconciliation
- [ ] 7. Dashboard et Telegram
- [ ] 8. Déploiement VPS et monitoring
