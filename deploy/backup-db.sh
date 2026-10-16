#!/bin/bash
set -euo pipefail

# =============================================================================
# Landline — PostgreSQL backup to S3
# =============================================================================
# Dumps the entire landline_db (every table, including the knowledge base:
# industry_contacts, knowledge_documents, plus all sequences/contacts/deals),
# gzips it, uploads to S3, and prunes old local copies.
#
# A full pg_dump covers any new table automatically — no per-table config.
#
# Runs on the EC2 host via the systemd timer (landline-backup.timer), daily.
# Usage:  ./backup-db.sh
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANDLINE_DIR="${LANDLINE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.production}"
IMAGE_ENV_FILE="${IMAGE_ENV_FILE:-.image.env}"
BACKUP_DIR="$LANDLINE_DIR/backups/db"
S3_BUCKET="${LANDLINE_BACKUP_BUCKET:-landline-backups-235494810658}"
DB_NAME="${POSTGRES_DB:-landline_db}"
DB_USER="${POSTGRES_USER:-landline}"
LOCAL_KEEP_DAYS="${LOCAL_KEEP_DAYS:-7}"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
ARCHIVE="landline_db_${TIMESTAMP}.sql.gz"

cd "$LANDLINE_DIR"
mkdir -p "$BACKUP_DIR"

compose() {
    local args=(docker compose)
    [ -f "$ENV_FILE" ] && args+=(--env-file "$ENV_FILE")
    [ -f "$IMAGE_ENV_FILE" ] && args+=(--env-file "$IMAGE_ENV_FILE")
    args+=(-f "$COMPOSE_FILE")
    "${args[@]}" "$@"
}

echo "[backup] Dumping $DB_NAME at $(date -u +%FT%TZ)"
# pg_dump runs inside the postgres container; stream the dump out and gzip on host.
compose exec -T postgres \
    pg_dump -U "$DB_USER" --no-owner --clean --if-exists "$DB_NAME" \
    | gzip -9 > "$BACKUP_DIR/$ARCHIVE"

SIZE=$(du -h "$BACKUP_DIR/$ARCHIVE" | cut -f1)
echo "[backup] Wrote $BACKUP_DIR/$ARCHIVE ($SIZE)"

# Refuse to upload an empty/truncated dump.
if [ ! -s "$BACKUP_DIR/$ARCHIVE" ] || [ "$(gzip -t "$BACKUP_DIR/$ARCHIVE" 2>&1 || echo bad)" = "bad" ]; then
    echo "[backup] ERROR: dump is empty or corrupt — not uploading" >&2
    exit 1
fi

if command -v aws &>/dev/null; then
    echo "[backup] Uploading to s3://$S3_BUCKET/db/$ARCHIVE"
    aws s3 cp "$BACKUP_DIR/$ARCHIVE" "s3://$S3_BUCKET/db/$ARCHIVE"
    echo "[backup] Upload complete"
else
    echo "[backup] WARNING: aws CLI not found — kept local copy only" >&2
fi

# Prune local copies older than LOCAL_KEEP_DAYS (S3 lifecycle handles remote retention).
find "$BACKUP_DIR" -name 'landline_db_*.sql.gz' -mtime "+$LOCAL_KEEP_DAYS" -delete
echo "[backup] Done."
