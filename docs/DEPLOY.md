# Déploiement sur le VPS

Le bot tourne sur un VPS Linux avec une IP fixe et aucun port public : tout passe par SSH (par clé) et par Tailscale.
Ces étapes sont manuelles : rien ici ne se lance tout seul.

## 1. Machine

- VPS de 2 vCPU et 4 Go de RAM, Debian 12 ou Ubuntu 24.04, hébergé en Suisse ou dans l'UE, avec une IP fixe.
  C'est cette IP qui sera liée à la clé API live.
- Crée un utilisateur non-root, par exemple `deploy`, et ajoute ta clé SSH publique.

```bash
# /etc/ssh/sshd_config
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
```

```bash
sudo systemctl restart ssh
sudo apt update && sudo apt install -y docker.io docker-compose-plugin chrony ufw gpg rclone unattended-upgrades
sudo usermod -aG docker deploy
sudo systemctl enable --now chrony   # horloge NTP : le bot refuse de démarrer si le décalage dépasse 2 s
```

## 2. Tailscale et pare-feu

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
tailscale ip -4                       # à mettre dans TAILSCALE_IP

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0
sudo ufw enable
```

Une fois que SSH par Tailscale fonctionne, ferme le port 22 public. Le dashboard est alors joignable uniquement
à `http://<ip-tailscale>:8000`.

> Docker contourne ufw pour les ports publiés. Ici, le port `8000` n'est publié que sur l'IP Tailscale
> (`${TAILSCALE_IP}:8000`), jamais sur `0.0.0.0`. Ne change pas cette ligne.

## 3. Code et secrets

```bash
git clone <repo> /opt/tradingbastien && cd /opt/tradingbastien
git checkout <tag>                    # on déploie toujours un tag Git
cp .env.example .env && chmod 600 .env
nano .env                             # clés démo d'abord ; la clé live se saisit ici, à la main, jamais ailleurs
```

## 4. Démarrage en démo

```bash
docker compose up -d --build
docker compose logs -f bot            # contrôle des permissions, horloge, réconciliation
```

Au démarrage, le bot refuse de tourner dans ces cas :

- la clé a une permission autre que `ContractTrade : Order + Position` (retrait, transfert, spot…) ;
- en live, la clé n'est pas liée à une IP ;
- l'horloge est décalée de plus de 2 s ;
- un plafond de risque est dépassé dans `.env` ;
- en live, `LIVE_CONFIRMED=yes` ou `MAX_CAPITAL_USDT` est absent.

## 5. Surveillance

- **Heartbeat** : crée un check sur healthchecks.io ou Uptime Kuma, avec une période de 1 min et une grâce de 5 min.
  Mets son URL dans `HEARTBEAT_URL`. Le service externe alerte si le bot se tait.
- **Telegram** : crée un bot avec @BotFather, puis renseigne `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID`.
  Le bot accepte `/status`, `/pause` et `/kill` (confirmation par `/kill CONFIRMER`).
  Le service `api` alerte aussi si le bot ne publie plus d'état depuis 5 min.

## 6. Sauvegardes

```bash
gpg --import ma-cle-publique.asc      # la clé PRIVÉE reste hors du VPS
rclone config                         # remote hors du VPS (B2, S3, Infomaniak Swiss Backup…)
# dans .env : BACKUP_GPG_RECIPIENT=... et BACKUP_REMOTE=remote:bucket/bot
sudo crontab -e
15 3 * * * /opt/tradingbastien/scripts/backup.sh >> /var/log/bot-backup.log 2>&1
```

Teste une restauration au moins une fois :

```bash
gpg -d bot-....sql.gz.gpg | gunzip | psql
```

## 7. Mises à jour

En live, une mise à jour ne se fait qu'avec le bot en pause :

1. Mets le bot en pause depuis le dashboard ou avec `/pause`. Les positions et leurs stops restent chez Bybit.
2. Lance `git fetch --tags && git checkout <nouveau-tag>`.
3. Lance `docker compose up -d --build`.
4. Contrôle les logs, puis reprends :

```bash
docker compose stop bot
docker compose run --rm bot python -m bot.admin resume
docker compose start bot
```

## 8. Sortir du mode HALT (après un kill switch ou un drawdown de 15 %)

Fais d'abord une revue : pourquoi le bot s'est-il arrêté ? Ensuite :

```bash
docker compose stop bot
docker compose run --rm bot python -m bot.admin clear-halt   # confirmation à taper en entier
docker compose run --rm bot python -m bot.admin resume
docker compose start bot
```

## 9. Passage en live (après go/no-go uniquement)

1. Le backtest passe tous les critères (`python -m bot.backtest run`).
2. Six semaines de démo se sont déroulées sans incident (zéro position sans stop, zéro bug d'exécution).
3. Crée un sous-compte Bybit dédié, en marge isolée et en mode one-way.
   Verse-lui à la main uniquement le capital prévu.
4. Crée la clé API du sous-compte avec seulement `Contract Trade : Order + Position`, liée à l'IP du VPS.
5. Dans `.env` : `MODE=live`, `LIVE_CONFIRMED=yes`, `MAX_CAPITAL_USDT=<montant>`, plus les clés live.
6. Lance `docker compose up -d` et surveille de près les premières entrées.
