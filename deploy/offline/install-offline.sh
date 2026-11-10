#!/usr/bin/env bash
# Install Smriti on an air-gapped machine from a bundle made by make-bundle.sh.
# Needs Docker with Compose v2 (and the NVIDIA container toolkit for GPU
# profiles). Makes no network connection outside this machine.
#
#   ./install-offline.sh [install-dir]      default: ./smriti
set -euo pipefail
cd "$(dirname "$0")"
BUNDLE="$PWD"
DEST="${1:-$PWD/smriti}"
say() { printf '\033[1;35m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing"

say "verifying the bundle"
sha256sum -c --quiet SHA256SUMS || die "checksum mismatch: the bundle was damaged or changed in transit"

say "loading container images (this takes a few minutes)"
docker load -i images.tar

say "unpacking Smriti into $DEST"
mkdir -p "$DEST"
tar -xf smriti-src.tar -C "$DEST"
cp site.env "$DEST/.env"
cd "$DEST"
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')}"

say "restoring the model cache"
docker compose create --no-build --pull never >/dev/null 2>&1 || true   # creates the volumes with compose labels
VOL="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" --filter label=com.docker.compose.volume=hf-cache | head -1)"
[[ -n "$VOL" ]] || die "could not create the hf-cache volume"
SMRITI_IMG="$(grep -E '^SMRITI_IMAGE=' .env | cut -d= -f2- || true)"
docker run --rm --network none -v "$VOL":/dst -v "$BUNDLE":/b:ro --entrypoint tar "${SMRITI_IMG:-safearly/smriti:local}" \
  -C /dst -xf /b/hf-cache.tar

say "starting Smriti (no build, no pulls)"
docker compose up -d --no-build --pull never
PORT="$(grep -E '^SMRITI_PORT=' .env | cut -d= -f2 || echo 8000)"
for i in $(seq 1 180); do
  if curl -fsS "http://127.0.0.1:${PORT:-8000}/api/health" >/dev/null 2>&1; then
    say "ready: http://127.0.0.1:${PORT:-8000}"; exit 0
  fi
  sleep 10
done
die "not ready after 30 minutes; see: docker compose logs --tail 50"
