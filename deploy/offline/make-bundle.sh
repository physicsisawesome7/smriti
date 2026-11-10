#!/usr/bin/env bash
# Build an offline bundle for an air-gapped site, on a machine WITH internet.
#
#   1. Run ./install.sh here once with the profile the site will use, and wait
#      until it says "ready": that downloads every model into the hf-cache volume.
#   2. Run: deploy/offline/make-bundle.sh [output-dir]
#   3. Carry the output folder to the site (disk, approved media) and run
#      ./install-offline.sh there. Nothing on the site contacts the internet.
#
# The bundle holds: every container image (docker save), the model cache, the
# Smriti source at this commit, a .env with HF_HUB_OFFLINE=1, and SHA256SUMS.
set -euo pipefail
cd "$(dirname "$0")/../.."
say() { printf '\033[1;35m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die "no .env: run ./install.sh with the site's profile first"
command -v docker >/dev/null || die "docker is missing"
OUT="${1:-smriti-offline-$(git describe --tags --always 2>/dev/null || date +%Y%m%d)}"
mkdir -p "$OUT"
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')}"

say "building the Smriti image"
docker compose build

say "saving container images"
mapfile -t IMAGES < <(docker compose config --images | sort -u)
[[ ${#IMAGES[@]} -gt 0 ]] || die "compose lists no images"
for img in "${IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
done
printf '%s\n' "${IMAGES[@]}" > "$OUT/images.txt"
docker save -o "$OUT/images.tar" "${IMAGES[@]}"

say "exporting the model cache"
VOL="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" --filter label=com.docker.compose.volume=hf-cache | head -1)"
[[ -n "$VOL" ]] || die "no hf-cache volume for project '$PROJECT': has ./install.sh finished here?"
SMRITI_IMG="$(grep -E '^SMRITI_IMAGE=' .env | cut -d= -f2- || true)"
docker run --rm -v "$VOL":/src:ro -v "$PWD/$OUT":/out --entrypoint tar "${SMRITI_IMG:-safearly/smriti:local}" \
  -C /src -cf /out/hf-cache.tar .

say "copying source and settings"
git archive --format=tar -o "$OUT/smriti-src.tar" HEAD
sed -e 's/^HF_HUB_OFFLINE=.*/HF_HUB_OFFLINE=1/' -e '/^HF_TOKEN=/s/=.*/=/' .env > "$OUT/site.env"
grep -q '^HF_HUB_OFFLINE=' "$OUT/site.env" || echo 'HF_HUB_OFFLINE=1' >> "$OUT/site.env"
cp deploy/offline/install-offline.sh "$OUT/"
chmod +x "$OUT/install-offline.sh"

say "writing checksums"
(cd "$OUT" && sha256sum images.tar hf-cache.tar smriti-src.tar site.env install-offline.sh images.txt > SHA256SUMS)
du -sh "$OUT"
say "done: copy '$OUT' to the site and run ./install-offline.sh there"
