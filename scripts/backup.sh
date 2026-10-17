#!/usr/bin/env bash
# Dump PostgreSQL quotidien, chiffré, copié hors du VPS.
# Cron (root) : 15 3 * * * /opt/tradingbastien/scripts/backup.sh >> /var/log/bot-backup.log 2>&1
# Prérequis : une clé publique GPG importée (BACKUP_GPG_RECIPIENT) et un remote rclone (BACKUP_REMOTE).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a
: "${BACKUP_GPG_RECIPIENT:?BACKUP_GPG_RECIPIENT manquant dans .env}"
: "${BACKUP_REMOTE:?BACKUP_REMOTE manquant dans .env (ex. b2:mon-bucket/bot)}"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="/var/backups/bot/bot-${stamp}.sql.gz.gpg"
mkdir -p /var/backups/bot
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-bot}" "${POSTGRES_DB:-bot}" \
  | gzip -9 \
  | gpg --batch --yes --trust-model always --encrypt --recipient "$BACKUP_GPG_RECIPIENT" --output "$out"
rclone copy "$out" "$BACKUP_REMOTE"
# Conserve 14 jours en local.
find /var/backups/bot -name 'bot-*.sql.gz.gpg' -mtime +14 -delete
echo "$(date -u +%FT%TZ) sauvegarde OK : $out"
