# Cahier des charges — Bot de trading Bybit

Oct 16, 2026 · @Bastien

## Objectif et règles non négociables

Le bot trade des futures perpétuels USDT sur Bybit pour un seul utilisateur, avec un capital plafonné, et place la survie du capital avant le rendement. Claude Code écrit le code ; le bot tourne seul sur un VPS et applique uniquement les règles de ce document.

**Périmètre v1**

- Exchange : Bybit, API V5, futures perpétuels linéaires USDT uniquement.
- Paires : BTCUSDT et ETHUSDT (liquidité maximale, spreads faibles).
- Utilisateur : un seul. Aucune gestion de fonds de tiers.
- Modes : backtest, puis démo, puis live, dans cet ordre.

**Règles non négociables** (codées en dur, valables même si un paramètre est mal réglé)

1. Aucun ordre sans stop-loss posé chez Bybit au moment de l'entrée.
2. Le levier découle du sizing, il n'est jamais choisi à la main, et il est plafonné.
3. Pas de martingale, pas de moyenne à la baisse, pas de doublement après une perte.
4. Le module de risque a un droit de veto sur toute décision de la stratégie.
5. Aucune clé API avec droit de retrait ou de transfert.
6. Le passage en live exige les critères go/no-go et une confirmation manuelle.
7. En cas de doute (erreur API, état incohérent, données manquantes), le bot n'ouvre rien et alerte.

## Compte, sécurité et clés API

Le bot n'accède qu'à un sous-compte dédié, avec une clé limitée au trading de contrats et liée à l'IP du VPS. Le reste de l'argent reste hors de sa portée.

**Compte**

- Sous-compte Bybit dédié au bot. Seul le capital de trading y est versé, à la main, depuis le compte principal.
- Marge isolée, mode de position one-way (pas de hedge mode en v1).
- 2FA active sur le compte principal.

**Clé API live**

| Réglage | Valeur |
| --- | --- |
| Rattachement | Sous-compte du bot |
| Permissions | Contract Trade : Order + Position, rien d'autre |
| Interdit | Withdrawal, Account Transfer, Subaccount Transfer, Spot |
| IP | IP fixe du VPS uniquement |

Une clé sans IP liée devient invalide après 90 jours, et toute clé expire 7 jours après un changement de mot de passe ([doc Bybit](https://bybit-exchange.github.io/doc-v3-offline/account-asset/modify-sub-apikey)). Au démarrage, le bot appelle `GET /v5/user/query-api` : il refuse de démarrer si une permission interdite est active, et alerte quand l'expiration approche ([doc Bybit](https://bybit-exchange.github.io/docs/v5/user/apikey-info)).

**Clé démo**

- Le Demo Trading est un compte isolé avec son propre user ID et ses propres clés ; REST sur `https://api-demo.bybit.com` ([doc Bybit](https://bybit-exchange.github.io/docs/v5/demo)).
- Les prix sont ceux du mainnet ; le WebSocket Trade API n'existe pas en démo, donc les ordres passent par REST ([NautilusTrader](https://nautilustrader.io/docs/latest/integrations/bybit/)).

**Secrets**

- Fichier `.env` hors Git, `.env.example` avec des valeurs factices.
- Clés jamais écrites dans les logs, jamais affichées dans le dashboard.
- Claude Code travaille avec les clés démo ; la clé live est saisie à la main sur le VPS.

## Gestion du risque

Chaque trade risque au maximum 1 % du capital du sous-compte, et le levier effectif ne dépasse jamais 3x. La taille de position découle de la distance au stop, jamais l'inverse.

**Taille de position**

```latex
\text{qty} = \frac{\text{equity} \times r}{|\text{entry} - \text{stop}| + \text{entry} \times 2 \times \text{fee}_{\text{taker}}}
```

Puis levier effectif = qty × entry / equity. S'il dépasse le plafond, la quantité est réduite ; elle n'est jamais augmentée. Exemple hors frais : 1000 USDT de capital, 1 % de risque, BTC à 60 000 avec stop à 58 800 donne environ 0,0083 BTC, soit 500 USDT de notionnel et 0,5x de levier effectif.

**Limites**

| Paramètre | Défaut | Plafond codé en dur |
| --- | --- | --- |
| Risque par trade (% equity) | 1 % | 2 % |
| Levier effectif (notionnel / equity) | 2x | 3x |
| Levier réglé chez Bybit (marge isolée) | 3x | 3x |
| Positions ouvertes en même temps | 2 | 3 |
| Risque total ouvert | 2 % | 3 % |
| Perte journalière (UTC) | -3 % : pause jusqu'à minuit | -3 % |
| Perte hebdomadaire | -6 % : pause jusqu'au lundi + revue | -6 % |
| Drawdown depuis le pic | -15 % : arrêt complet | -15 % |
| Pertes consécutives | 4 : pause 24 h | 4 |

Les plafonds sont des constantes du module de risque. La config peut les abaisser, jamais les relever, et un test le vérifie.

**Stop-loss**

- Le stop part dans le même ordre que l'entrée : `stopLoss`, `slTriggerBy=MarkPrice`, `tpslMode=Full` ([doc Bybit](https://bybit-exchange.github.io/docs/zh-TW/api-explorer/v5/trade/create-order)).
- Position remplie sans stop visible chez Bybit après 5 s : clôture immédiate au marché et alerte.
- Un stop ne s'éloigne jamais ; il peut seulement se rapprocher (break-even, trailing via `/v5/position/trading-stop`).
- Le prix de liquidation doit être au moins 3 fois plus loin que le stop, sinon le trade est refusé.
- Frais et funding sont comptés dans le P&L réel et dans les backtests.

**Kill switch**

- Bouton du dashboard et commande Telegram `/kill`.
- Effet : annule tous les ordres, ferme toutes les positions au marché, passe le bot en mode HALT.
- Seul un redémarrage manuel, après confirmation, sort du mode HALT.

## Stratégie de base

La v1 utilise une stratégie de suivi de tendance simple, sur bougies 4 h, pour valider toute la chaîne. Elle ne promet aucun gain : elle ne passe en live que si elle franchit les critères go/no-go.

**Règles (BTCUSDT et ETHUSDT, une position max par paire)**

| Élément | Règle |
| --- | --- |
| Filtre de tendance | Long seulement si EMA 50 > EMA 200 ; short seulement si EMA 50 < EMA 200 |
| Entrée | Clôture 4 h au-dessus du plus haut des 20 dernières bougies (long), sous le plus bas (short) |
| Stop initial | 2 × ATR(14) depuis l'entrée |
| Break-even | Stop déplacé à l'entrée une fois +1R atteint |
| Sortie | Trailing stop à 3 × ATR(14) après +1R, ou clôture sous le plus bas des 10 dernières bougies (long) |
| Ordre d'entrée | Au marché, juste après la clôture de la bougie |

**Interface**

- Une stratégie renvoie un signal : paire, sens, prix d'entrée, stop. Rien d'autre.
- Le module de risque calcule la taille ou oppose son veto.
- Une nouvelle stratégie s'ajoute comme un plugin et repasse par backtest puis démo.

**Interdit en v1**

- Grille, martingale, moyenne à la baisse.
- Décisions prises par une IA : impossible à backtester proprement.
- Trading sur des altcoins peu liquides.

## Architecture technique

Un dépôt Python, trois services Docker et un chemin unique pour chaque ordre : données, stratégie, risque, exécution. Le même code de stratégie et de risque sert en backtest, en démo et en live.

&#91;embedded content: architecture · 7 composants, 1 point de contrôle\]

Le module de risque est le seul passage vers l'exécution ; le dashboard et Telegram ne peuvent que mettre en pause ou tout couper.

**Modes**

| Mode | Données | Ordres | Sert à |
| --- | --- | --- | --- |
| Backtest | Historique Bybit (bougies, funding) | Simulés avec frais, slippage, funding | Valider la stratégie |
| Démo | Prix mainnet en temps réel | `api-demo.bybit.com`, argent fictif | Valider l'exécution |
| Live | Prix mainnet en temps réel | `api.bybit.com`, sous-compte réel | Après go/no-go et confirmation manuelle |

Le mode se choisit par variable d'environnement. Le live refuse de démarrer sans `LIVE_CONFIRMED=yes` et un capital maximal déclaré.

**Arborescence**

```text
bot/
  exchange/    client Bybit (REST, WebSocket)
  data/        bougies, funding, contrôles
  strategy/    stratégies (plugins)
  risk/        sizing, plafonds, kill switch
  execution/   ordres, stops, réconciliation
  backtest/    moteur et rapports
api/           FastAPI, dashboard, Telegram
tests/
docs/SPEC.md
CLAUDE.md
```

## Dashboard

Le dashboard est privé, joignable uniquement par Tailscale, et en lecture seule sauf deux actions : pause et kill switch. Aucun réglage de risque ne se relève depuis l'interface.

| Page | Contenu |
| --- | --- |
| Vue d'ensemble | Equity, P&L jour / semaine / total, drawdown vs limite, statut (RUN, PAUSE, HALT), mode (démo ou live) |
| Positions | Entrée, stop, prix de liquidation, taille, levier effectif, P&L latent, risque en USDT |
| Historique | Chaque trade : date, paire, sens, résultat en R, frais, funding, raison de sortie |
| Risque | Jauges des limites (jour, semaine, drawdown, risque ouvert), pertes consécutives |
| Performance | Courbe d'equity, drawdown, taux de réussite, profit factor, R moyen, par stratégie |
| Journal | Logs, alertes, erreurs API, réconciliations |
| Réglages | Paramètres en lecture seule, plafonds affichés |

**Actions**

- Pause : le bot n'ouvre plus rien, les positions et leurs stops restent en place.
- Kill switch : double confirmation, puis comportement décrit dans Gestion du risque.

**Alertes Telegram**

- Entrée, sortie, stop touché, limite atteinte.
- Erreur API, écart de réconciliation, bot silencieux depuis plus de 5 min.
- Résumé quotidien : P&L, equity, trades du jour.

## Robustesse et tests

Bybit fait toujours foi : au démarrage et toutes les 60 s, le bot compare son état à celui de l'exchange, et n'ouvre rien tant qu'un écart n'est pas résolu.

**Cas à gérer**

| Situation | Comportement attendu |
| --- | --- |
| Redémarrage ou crash | État reconstruit depuis Bybit (positions, ordres, stops, solde) puis la base |
| WebSocket coupé | Reconnexion avec backoff ; plus de 60 s sans données = aucune nouvelle entrée |
| Erreur API ou rate limit | Retry avec backoff exponentiel ; `orderLinkId` unique par signal pour éviter les doublons |
| Remplissage partiel | Taille recalculée depuis les exécutions réelles, stop sur toute la position |
| Bougie incomplète ou trou de données | Signal ignoré, alerte |
| Horloge décalée | Synchro NTP ; `recv_window` court sur les requêtes signées |
| Quantité ou prix hors règles | Arrondi selon `qtyStep` et `tickSize` de `/v5/market/instruments-info` ; sous le minimum = trade refusé |

**Tests obligatoires**

- Module de risque : 100 % de couverture, plus des tests de propriété (Hypothesis) prouvant qu'aucune entrée ne dépasse les plafonds.
- Sizing : stop très proche, capital minimal, quantité minimale Bybit, arrondis.
- Pannes simulées : crash entre l'entrée et la pose du stop, WebSocket coupé, erreurs API, remplissage partiel.
- CI GitHub Actions : tests, lint et typage à chaque commit ; aucun déploiement si un test échoue.

## Backtest et critères go/no-go

Aucun franc réel n'est engagé avant que la stratégie ait passé le backtest puis six semaines de démo sans incident. Un échec renvoie à la phase précédente.

**Exigences du backtest**

- Données Bybit : bougies `/v5/market/kline` et funding `/v5/market/funding/history`, au moins 3 ans pour couvrir hausse, baisse et range.
- Frais taker réels, slippage de 0,05 % par côté (hypothèse à recaler en démo), funding historique.
- Aucun look-ahead : signal sur bougie close, exécution à l'ouverture suivante.
- Walk-forward : réglage sur 70 % des données, validation sur les 30 % restants, jamais vus.
- Monte Carlo sur l'ordre des trades pour estimer le pire drawdown probable.
- Même code de stratégie et de risque en backtest, démo et live.

**Passage d'une phase à l'autre**

| Phase | Durée min. | Argent | Condition pour passer à la suivante |
| --- | --- | --- | --- |
| Backtest | 3 ans de données | Aucun | ≥ 100 trades, profit factor ≥ 1,3 après frais, drawdown max ≤ 20 %, résultat positif hors échantillon |
| Démo Bybit | 6 semaines | Fictif | Zéro position sans stop, zéro bug d'exécution, résultats dans la fourchette du backtest |
| Live réduit | 3 mois | Petit montant, perte totale acceptable | Mêmes critères, slippage réel mesuré et intégré au backtest |
| Live normal | Continu | Capital fixé d'avance, jamais rechargé après une perte | Revue mensuelle des métriques |

Interdit : retoucher les paramètres jusqu'à ce que le backtest passe. C'est du sur-ajustement, et il se paie en live.

## Déploiement et monitoring

Le bot tourne sur un VPS Linux avec IP fixe, sans aucun port public : tout passe par SSH par clé et par Tailscale. La stratégie 4 h ne demande pas de faible latence.

- Machine : VPS 2 vCPU, 4 Go de RAM, IP fixe (celle liée à la clé API), hébergé en Suisse ou dans l'UE.
- Services Docker Compose : `bot`, `api` (dashboard), `db` (PostgreSQL), redémarrage automatique.
- Pare-feu : tout fermé sauf Tailscale ; SSH par clé uniquement, root désactivé.
- Heure : chrony (NTP).
- Sauvegardes : dump PostgreSQL quotidien, chiffré, copié hors du VPS.
- Logs : JSON avec rotation, sans aucune clé ni secret.
- Heartbeat : ping toutes les minutes vers un service de surveillance externe ; silence = alerte Telegram.
- Mises à jour : déploiement par tag Git, en live uniquement avec le bot en pause.

## Plan de développement pour Claude Code

Claude Code avance étape par étape, et chaque étape se termine par ses tests au vert avant la suivante. Le module de risque est écrit avant toute stratégie.

1. Squelette du repo : arborescence, config, `.env.example`, Docker Compose, CI, ruff, mypy.
2. Client Bybit : wrapper pybit REST + WebSocket, environnements démo et live, lecture des règles d'instruments, contrôle des permissions de la clé.
3. Module de risque : sizing, plafonds, limites, veto, kill switch, avec tous ses tests.
4. Données et backtester : téléchargement des bougies et du funding, moteur avec frais, slippage et funding.
5. Stratégie de base et rapport de backtest (HTML : equity, drawdown, métriques, walk-forward).
6. Exécution et réconciliation, testées en démo.
7. Dashboard et alertes Telegram.
8. Déploiement sur le VPS et monitoring.
9. Six semaines de démo, puis revue go/no-go.
10. Live réduit, sur décision manuelle.

**Fichier `CLAUDE.md` à placer à la racine du repo**

```markdown
# Règles du projet
- La spécification fait foi : docs/SPEC.md (ce cahier des charges).
- Ne jamais relever un plafond de risque ni contourner le module de risque.
- Aucun ordre sans stop-loss attaché. Aucun code de retrait ou de transfert.
- Ne jamais lire, afficher ni écrire de vraie clé API. Travailler en démo uniquement.
- Une étape à la fois ; tests, lint et typage au vert avant de passer à la suivante.
- Toute règle ambiguë : demander avant de coder.
```

Stack : Python 3.12, pybit, FastAPI, PostgreSQL, pandas pour le backtest, pytest et Hypothesis. Front du dashboard au choix (HTML + htmx ou React), graphiques avec Lightweight Charts.

## Points fiscaux et légaux en Suisse

Ce bot ne remplit pas les critères suisses qui excluent le commerce professionnel de titres : ses gains pourraient être imposés comme revenu d'activité indépendante. À valider avec une fiduciaire ou le Service cantonal des contributions du Valais avant le live.

**Critères « safe harbour », tous requis** (pratique cantonale basée sur la [circulaire AFC n° 36](https://www.estv.admin.ch/dam/de/sd-web/oOz28af293pZ/dbst-ks-2012-1-036-d-de.pdf), ici selon [la pratique thurgovienne](https://steuerpraxis.tg.ch/steuerpraxis/2026-09/stp-20-nr-2-gewerbsmassiger-handel-mit-wertschriften-gold-devisen-oder-kryptowahrungen))

| Critère | Le bot le respecte ? |
| --- | --- |
| Durée de détention d'au moins un an | Non : positions de quelques jours |
| Volume annuel ≤ 5 fois le portefeuille de début d'année | Probablement non, vu le notionnel tradé |
| Gains < 50 % du revenu imposable | Oui, a priori |
| Pas de lien avec la profession | Oui |
| Pas de financement par emprunt | À clarifier pour le levier |
| Dérivés seulement pour couvrir ses propres positions | Non : futures spéculatifs |

Si l'activité est qualifiée de professionnelle, les gains entrent dans le revenu d'activité indépendante, comme pour la crypto en général ([source](https://steuerpraxis.tg.ch/steuerpraxis/2026-09/stp-20-nr-2-gewerbsmassiger-handel-mit-wertschriften-gold-devisen-oder-kryptowahrungen)). Les cotisations AVS peuvent suivre.

**Conséquences pour le projet**

- Le bot exporte un relevé annuel : chaque trade, frais, funding, P&L en USDT et en CHF au cours du jour.
- Le solde du sous-compte se déclare dans la fortune.
- Question ouverte : rattacher ou non cette activité à la raison individuelle existante.
- Le bot reste strictement personnel : gérer l'argent de tiers relèverait d'un tout autre cadre (FINMA).
- Risque de contrepartie : Bybit a subi un piratage de 1,4 milliard de dollars en février 2025, sans perte pour les clients ([CoinPerps](https://www.coinperps.com/zh/learn/bybit-restricted-countries)). Ne laisser chez Bybit que le capital de trading.
- Vérifier au KYC que le compte global est bien ouvert aux résidents suisses.

Ce document n'est ni un conseil fiscal ni un conseil financier.
