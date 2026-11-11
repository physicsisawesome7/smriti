#!/usr/bin/env bash
# Download GGUF weights for llama.cpp (the `llm-gguf` service and the
# benchmarks) into the model cache volume under gguf/, pinned to a Hugging Face
# commit and checked against the published SHA-256. Re-running skips files that
# are already there. The offline bundle exports this volume, so air-gapped
# installs get the weights too.
#
#   deploy/bench/get-gguf.sh            # all three (about 5 GB)
#   deploy/bench/get-gguf.sh 4b         # just Qwen3-4B Q4_K_M (or pass the file name)
#
# All three models are Apache-2.0 (Qwen3 model cards).
set -euo pipefail

#        key   repo                  commit                                    file                    sha256
MODELS=(
  "4b    Qwen/Qwen3-4B-GGUF   bc640142c66e1fdd12af0bd68f40445458f3869b Qwen3-4B-Q4_K_M.gguf 7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
  "1.7b  Qwen/Qwen3-1.7B-GGUF 90862c4b9d2787eaed51d12237eafdfe7c5f6077 Qwen3-1.7B-Q8_0.gguf 061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"
  "0.6b  Qwen/Qwen3-0.6B-GGUF 23749fefcc72300e3a2ad315e1317431b06b590a Qwen3-0.6B-Q8_0.gguf 9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"
)
want=${1:-all}
found=0

PROJECT=${COMPOSE_PROJECT_NAME:-smriti}
VOL=$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" --filter label=com.docker.compose.volume=hf-cache | head -1)
VOL=${VOL:-${PROJECT}_hf-cache}
docker volume inspect "$VOL" >/dev/null 2>&1 || docker volume create "$VOL" >/dev/null
for m in "${MODELS[@]}"; do
  read -r key repo rev file sha <<<"$m"
  [[ $want == all || $want == "$key" || $want == "$file" ]] || continue
  found=1
  echo "-- $file"
  MSYS_NO_PATHCONV=1 docker run --rm -v "$VOL:/m" alpine:3.20 sh -euc "
    mkdir -p /m/gguf && cd /m/gguf
    [ -s '$file' ] || { wget -q -O '$file.part' 'https://huggingface.co/$repo/resolve/$rev/$file' && mv '$file.part' '$file'; }
    echo '$sha  $file' | sha256sum -c -
  "
done
(( found )) || { echo "get-gguf: no pinned model matches '$want'; put your own GGUF in the cache volume under gguf/" >&2; exit 1; }
