#!/usr/bin/env bash
# Rebuild vendor/saaf-*.whl from the Saaf commit pinned in pyproject.toml and
# check it matches the vendored file byte for byte. The build is reproducible
# (fixed SOURCE_DATE_EPOCH), so the hash in the Dockerfile can be verified by
# anyone. Needs git and uv (or `pip install build` and change the build line).
#
#   vendor/build-saaf.sh            verify
#   vendor/build-saaf.sh --update   replace the vendored wheel
set -euo pipefail
cd "$(dirname "$0")/.."
SHA="$(grep -oE 'Saaf@[0-9a-f]{40}' pyproject.toml | cut -d@ -f2)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
git clone --quiet https://github.com/physicsisawesome7/Saaf "$TMP/saaf"
git -C "$TMP/saaf" archive "$SHA" packages/saaf-core | tar -x -C "$TMP"
(cd "$TMP/packages/saaf-core" && SOURCE_DATE_EPOCH=1762214400 uv build --wheel -o "$TMP/dist" -q)
NEW="$(ls "$TMP"/dist/*.whl)"
echo "built:    $(sha256sum "$NEW" | cut -d' ' -f1)  $(basename "$NEW")  (Saaf $SHA)"
for f in vendor/saaf-*.whl; do [[ -e "$f" ]] && echo "vendored: $(sha256sum "$f" | cut -d' ' -f1)  $(basename "$f")"; done
if [[ "${1:-}" == "--update" ]]; then
  rm -f vendor/saaf-*.whl && cp "$NEW" vendor/
  echo "updated: now set the hash in the Dockerfile to the 'built' value"
fi
