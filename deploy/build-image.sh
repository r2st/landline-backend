#!/usr/bin/env bash
set -euo pipefail

# Build and push the production Landline runtime image from a workstation or CI.
# The production server should only pull this image.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-ghcr.io/r2st/landline-backend}"
PLATFORMS="${PLATFORMS:-linux/arm64}"
PUSH="${PUSH:-1}"
OUTPUT="${OUTPUT:-}"
BUILDER_NAME="${BUILDER_NAME:-landline-builder}"
SHORT_SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
IMAGE_TAG="${IMAGE_TAG:-sha-$SHORT_SHA}"
IMAGE="$REGISTRY_IMAGE:$IMAGE_TAG"
BUILD_DATE="$(date -u +%FT%TZ)"

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required to build images" >&2
    exit 1
fi

cd "$ROOT"

if docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    docker buildx use "$BUILDER_NAME" >/dev/null
else
    docker buildx create --use --name "$BUILDER_NAME" >/dev/null
fi
args=(
    --platform "$PLATFORMS"
    --build-arg "VCS_REF=$(git rev-parse HEAD)"
    --build-arg "BUILD_DATE=$BUILD_DATE"
    --tag "$IMAGE"
)

if [ -n "$OUTPUT" ]; then
    args+=(--output "type=docker,dest=$OUTPUT")
elif [ "$PUSH" = "1" ]; then
    args+=(--push)
else
    args+=(--load)
fi

docker buildx build "${args[@]}" .

echo "$IMAGE"
