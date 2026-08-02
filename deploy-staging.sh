#!/usr/bin/env bash
# deploy-staging.sh — Safe deploy to staging EC2 (100.30.136.86)
# Handles the .env.production deletion risk from recent git tracking removal
#
# Usage:
#   SSH:  ssh ec2-user@100.30.136.86 'bash -s' < deploy-staging.sh
#   SSM:  aws ssm send-command --instance-ids <INSTANCE_ID> --document-name AWS-RunShellScript --parameters commands="$(cat deploy-staging.sh)"
#   Or just: ssh ec2-user@100.30.136.86   then paste/run this

set -euo pipefail

DEPLOY_DIR="/opt/landline"
COMPOSE_FILE="docker-compose.prod.yml"
HEALTH_URL="http://127.0.0.1:8001/health"

echo "=========================================="
echo "  Landline Staging Deploy — $(date)"
echo "=========================================="

cd "$DEPLOY_DIR"

# ── Step 1: Pre-flight checks ──
echo ""
echo "[1/8] Pre-flight checks..."

if [ ! -f .env.production ]; then
    echo "FATAL: .env.production does not exist at $DEPLOY_DIR"
    echo "       Cannot proceed — the app needs this file to boot."
    exit 1
fi

echo "  .env.production exists ($(wc -l < .env.production) lines)"
echo "  Current branch: $(git rev-parse --abbrev-ref HEAD)"
echo "  Current commit: $(git rev-parse --short HEAD)"

# ── Step 2: Back up .env.production ──
echo ""
echo "[2/8] Backing up .env.production..."
cp .env.production .env.production.bak
echo "  Backed up to .env.production.bak"

# Also stash any local changes to .env.production so git pull won't conflict
# (git may try to delete it if the recent commit removed it from tracking)
git stash push -m "deploy-backup-$(date +%s)" -- .env.production 2>/dev/null || true

# ── Step 3: git pull ──
echo ""
echo "[3/8] Pulling latest code from main..."
git pull origin main
echo "  Now at commit: $(git rev-parse --short HEAD)"

# ── Step 4: Restore .env.production ──
echo ""
echo "[4/8] Restoring .env.production..."
if [ ! -f .env.production ]; then
    echo "  .env.production was deleted by git pull — restoring from backup"
    cp .env.production.bak .env.production
else
    echo "  .env.production survived git pull — keeping it"
fi

# Verify it has content
if [ ! -s .env.production ]; then
    echo "FATAL: .env.production is empty after restore!"
    echo "       Restoring from backup..."
    cp .env.production.bak .env.production
fi

# Verify SECRET_KEY is present (required by recent changes)
if ! grep -q "SECRET_KEY" .env.production; then
    echo ""
    echo "WARNING: SECRET_KEY not found in .env.production"
    echo "  The app now requires SECRET_KEY to boot."
    echo "  Add it manually:  echo 'SECRET_KEY=your-secret-here' >> .env.production"
    echo "  Then re-run docker compose up."
    echo ""
fi

echo "  .env.production restored ($(wc -l < .env.production) lines)"

# ── Step 5: Ensure .env.production is in .gitignore ──
echo ""
echo "[5/8] Ensuring .env.production is gitignored..."
if ! grep -q "^\.env\.production$" .gitignore 2>/dev/null; then
    echo ".env.production" >> .gitignore
    echo "  Added .env.production to .gitignore"
else
    echo "  Already in .gitignore"
fi

# ── Step 6: Docker build and restart ──
echo ""
echo "[6/8] Building and restarting containers..."
docker compose -f "$COMPOSE_FILE" up -d --build
echo "  Containers starting..."

# Wait for postgres to be healthy before running migrations
echo "  Waiting for postgres to be healthy..."
for i in $(seq 1 30); do
    if docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U landline -d landline_db > /dev/null 2>&1; then
        echo "  Postgres is ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "FATAL: Postgres did not become healthy in 30 seconds"
        docker compose -f "$COMPOSE_FILE" logs postgres --tail=20
        exit 1
    fi
    sleep 1
done

# ── Step 7: Run Alembic migrations ──
echo ""
echo "[7/8] Running Alembic migrations..."
docker compose -f "$COMPOSE_FILE" exec -T api alembic upgrade head
echo "  Migrations complete"

# ── Step 8: Verify health ──
echo ""
echo "[8/8] Verifying health endpoint..."
sleep 5  # Give uvicorn a moment to reload

for i in $(seq 1 15); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "  Health check PASSED (HTTP 200)"
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo "  Health check FAILED after 15 attempts (last HTTP code: $HTTP_CODE)"
        echo "  Checking container logs..."
        docker compose -f "$COMPOSE_FILE" logs api --tail=30
        exit 1
    fi
    echo "  Attempt $i: HTTP $HTTP_CODE — retrying in 3s..."
    sleep 3
done

# ── Final status ──
echo ""
echo "=========================================="
echo "  DEPLOY COMPLETE"
echo "=========================================="
echo "  Commit:  $(git rev-parse --short HEAD)"
echo "  Branch:  $(git rev-parse --abbrev-ref HEAD)"
echo "  .env.production: $(wc -l < .env.production) lines"
echo ""
echo "  Container status:"
docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}"
echo ""
echo "  Cleanup: rm -f .env.production.bak"
echo "=========================================="
