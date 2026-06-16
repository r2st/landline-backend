#!/bin/bash
set -euo pipefail

# =============================================================================
# Landline Backend — Deploy / Update Script
# =============================================================================
# Pull latest code, rebuild containers, run migrations, restart.
# Includes rollback on migration failure.
#
# Usage:  ./deploy.sh [--rollback]
# =============================================================================

LANDLINE_DIR="/opt/landline"
COMPOSE_FILE="docker-compose.prod.yml"
BACKUP_DIR="$LANDLINE_DIR/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

cd "$LANDLINE_DIR"

# --- Rollback mode ---
if [ "${1:-}" = "--rollback" ]; then
    echo "Rolling back to previous version..."
    if [ -f "$BACKUP_DIR/last_good_commit" ]; then
        COMMIT=$(cat "$BACKUP_DIR/last_good_commit")
        git checkout "$COMMIT"
        docker compose -f "$COMPOSE_FILE" up -d --build api celery-worker
        echo "Rolled back to $COMMIT"
    else
        echo "ERROR: No rollback point found"
        exit 1
    fi
    exit 0
fi

echo "========================================"
echo "  Landline Backend — Deploy"
echo "  $(date)"
echo "========================================"

# --- 1. Save current commit as rollback point ---
echo ""
echo "[1/5] Saving rollback point..."
CURRENT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "initial")
echo "$CURRENT_COMMIT" > "$BACKUP_DIR/last_good_commit"
echo "  Rollback: $CURRENT_COMMIT"

# --- 2. Pull latest code ---
echo ""
echo "[2/5] Pulling latest code..."
git fetch origin
git pull origin main
NEW_COMMIT=$(git rev-parse HEAD)
echo "  Deployed: $NEW_COMMIT"

if [ "$CURRENT_COMMIT" = "$NEW_COMMIT" ]; then
    echo "  No new changes. Rebuilding anyway..."
fi

# --- 3. Rebuild and restart containers ---
echo ""
echo "[3/5] Building and restarting containers..."
docker compose -f "$COMPOSE_FILE" build api celery-worker
docker compose -f "$COMPOSE_FILE" up -d

# Wait for API to be healthy
echo "  Waiting for health check..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8001/health > /dev/null 2>&1; then
        echo "  API is healthy!"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ERROR: API failed health check after 30s"
        echo "  Rolling back..."
        echo "$CURRENT_COMMIT" > "$BACKUP_DIR/last_good_commit"
        git checkout "$CURRENT_COMMIT"
        docker compose -f "$COMPOSE_FILE" up -d --build api celery-worker
        exit 1
    fi
    sleep 1
done

# --- 4. Run database migrations ---
echo ""
echo "[4/5] Running database migrations..."
if docker exec landline-api alembic upgrade head; then
    echo "  Migrations complete"
else
    echo "  ERROR: Migration failed! Rolling back..."
    docker exec landline-api alembic downgrade -1 2>/dev/null || true
    git checkout "$CURRENT_COMMIT"
    docker compose -f "$COMPOSE_FILE" up -d --build api celery-worker
    echo "$CURRENT_COMMIT" > "$BACKUP_DIR/last_good_commit"
    exit 1
fi

# --- 5. Copy nginx config if changed ---
echo ""
echo "[5/5] Checking nginx config..."
if [ -f deploy/nginx-landline.conf ]; then
    cp deploy/nginx-landline.conf /etc/nginx/sites-available/landline
    ln -sf /etc/nginx/sites-available/landline /etc/nginx/sites-enabled/landline
    nginx -t && systemctl reload nginx
    echo "  Nginx reloaded"
fi

# --- Done ---
echo ""
echo "========================================"
echo "  Deploy complete!"
echo "  Commit: $NEW_COMMIT"
echo "  Time: $(date)"
echo "========================================"

# Log the deploy
echo "$TIMESTAMP | $NEW_COMMIT | success" >> "$BACKUP_DIR/deploy_log.txt"
