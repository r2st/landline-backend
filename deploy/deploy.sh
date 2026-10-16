#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Landline Backend - Image Deploy / Update Script
# =============================================================================
# Pull a prebuilt image, run migrations with that image, then restart services.
# The production server should not build application code.
#
# Usage:
#   LANDLINE_IMAGE=ghcr.io/r2st/prospect-backend:sha-abc123 ./deploy/deploy.sh
#   IMAGE_TAG=sha-abc123 ./deploy/deploy.sh
#   ./deploy/deploy.sh --rollback
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANDLINE_DIR="${LANDLINE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.production}"
IMAGE_ENV_FILE="${IMAGE_ENV_FILE:-.image.env}"
BACKUP_DIR="${BACKUP_DIR:-$LANDLINE_DIR/backups}"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-ghcr.io/r2st/prospect-backend}"
DEPLOY_SERVICES="${DEPLOY_SERVICES:-celery-worker}"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)

cd "$LANDLINE_DIR"
mkdir -p "$BACKUP_DIR"

log() {
    printf '\n[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

compose() {
    local args=(docker compose)
    [ -f "$ENV_FILE" ] && args+=(--env-file "$ENV_FILE")
    [ -f "$IMAGE_ENV_FILE" ] && args+=(--env-file "$IMAGE_ENV_FILE")
    args+=(-f "$COMPOSE_FILE")
    "${args[@]}" "$@"
}

resolve_image() {
    if [ -n "${LANDLINE_IMAGE:-}" ]; then
        printf '%s\n' "$LANDLINE_IMAGE"
        return
    fi
    if [ -n "${IMAGE_TAG:-}" ]; then
        printf '%s:%s\n' "$REGISTRY_IMAGE" "$IMAGE_TAG"
        return
    fi
    die "set LANDLINE_IMAGE or IMAGE_TAG"
}

write_image_env() {
    local image="$1"
    printf 'LANDLINE_IMAGE=%s\n' "$image" > "$IMAGE_ENV_FILE"
}

current_container_image() {
    docker inspect -f '{{.Config.Image}}' landline-celery 2>/dev/null || true
}

wait_for_celery() {
    log "Waiting for Celery health"
    for _ in $(seq 1 60); do
        status=$(docker inspect -f '{{.State.Health.Status}}' landline-celery 2>/dev/null || true)
        if [ "$status" = "healthy" ]; then
            log "Celery is healthy"
            return 0
        fi
        sleep 2
    done
    docker logs landline-celery --tail 100 >&2 || true
    die "Celery did not become healthy"
}

wait_for_api() {
    log "Waiting for API health"
    for _ in $(seq 1 60); do
        if curl -sf http://127.0.0.1:8001/health >/dev/null 2>&1; then
            log "API is healthy"
            return 0
        fi
        sleep 1
    done
    docker logs landline-api --tail 100 >&2 || true
    die "API did not become healthy"
}

# --- Rollback mode ---
if [ "${1:-}" = "--rollback" ]; then
    [ -f "$BACKUP_DIR/previous_image" ] || die "no previous image recorded"
    ROLLBACK_IMAGE=$(cat "$BACKUP_DIR/previous_image")
    log "Rolling back to $ROLLBACK_IMAGE"
    write_image_env "$ROLLBACK_IMAGE"
    compose pull api celery-worker || true
    compose up -d --no-deps $DEPLOY_SERVICES
    case " $DEPLOY_SERVICES " in
        *" celery-worker "*) wait_for_celery ;;
    esac
    case " $DEPLOY_SERVICES " in
        *" api "*) wait_for_api ;;
    esac
    log "Rollback complete"
    exit 0
fi

IMAGE=$(resolve_image)
PREVIOUS_IMAGE=$(current_container_image)

log "Deploying $IMAGE"
[ -n "$PREVIOUS_IMAGE" ] && printf '%s\n' "$PREVIOUS_IMAGE" > "$BACKUP_DIR/previous_image"
write_image_env "$IMAGE"

log "Ensuring backing services are up"
compose up -d postgres redis

log "Pulling application image"
if [ "${SKIP_PULL:-0}" = "1" ]; then
    log "Skipping pull because SKIP_PULL=1"
else
    compose pull api celery-worker
fi

log "Running database migrations with the new image"
if ! compose run --rm api alembic upgrade head; then
    log "Migration failed; restoring previous image"
    if [ -n "$PREVIOUS_IMAGE" ]; then
        write_image_env "$PREVIOUS_IMAGE"
        compose up -d --no-deps $DEPLOY_SERVICES || true
    fi
    die "migration failed"
fi

log "Restarting application services: $DEPLOY_SERVICES"
compose up -d --no-deps $DEPLOY_SERVICES

case " $DEPLOY_SERVICES " in
    *" celery-worker "*) wait_for_celery ;;
esac
case " $DEPLOY_SERVICES " in
    *" api "*) wait_for_api ;;
esac

log "Checking nginx config"
if [ -f deploy/nginx-landline.conf ] && [ -d /etc/nginx/sites-available ]; then
    cp deploy/nginx-landline.conf /etc/nginx/sites-available/landline
    ln -sf /etc/nginx/sites-available/landline /etc/nginx/sites-enabled/landline
    nginx -t && systemctl reload nginx
    log "Nginx reloaded"
elif [ -f deploy/nginx-landline.conf ]; then
    log "Skipping nginx update; /etc/nginx/sites-available is not present on this host"
fi

log "Pruning unused images"
docker image prune -f

printf '%s\n' "$IMAGE" > "$BACKUP_DIR/current_image"
printf '%s | %s | previous=%s | services=%s | success\n' \
    "$TIMESTAMP" "$IMAGE" "${PREVIOUS_IMAGE:-none}" "$DEPLOY_SERVICES" >> "$BACKUP_DIR/deploy_log.txt"

log "Deploy complete: $IMAGE"
