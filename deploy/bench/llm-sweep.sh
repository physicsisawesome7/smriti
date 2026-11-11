#!/usr/bin/env bash
# Start one llama.cpp server configuration, sweep it with `smriti modelbench chat`,
# write the JSON, stop the server. Compare configurations by running it once each.
#
#   deploy/bench/llm-sweep.sh NAME "llama-server args" [modelbench chat args...]
#
#   deploy/bench/llm-sweep.sh 4b-q8kv-np8 \
#     "-m /models/gguf/Qwen3-4B-Q4_K_M.gguf -ngl 99 -fa on -ctk q8_0 -ctv q8_0 -np 8 -c 16384"
#
# Weights live in the model cache volume under gguf/ (deploy/bench/get-gguf.sh).
# Results go to bench-results/chat-NAME.json. Set PY to the Python that has
# Smriti installed (default: .venv), PORT to move the server off 8081.
set -euo pipefail
name=$1; server_args=$2; shift 2
PORT=${PORT:-8081}
PY=${PY:-.venv/Scripts/python}
[[ -x $PY ]] || PY=.venv/bin/python
IMAGE=${LLAMACPP_IMAGE:-ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:17934f68b6efc68671800735f25e209b489f47b610cfbb5c0a5c30965b54023c}

docker rm -f bench-llm >/dev/null 2>&1 || true
# shellcheck disable=SC2086
MSYS_NO_PATHCONV=1 docker run -d --name bench-llm --gpus all -p "127.0.0.1:${PORT}:8080" \
  -v "${HF_VOLUME:-smriti_hf-cache}:/models:ro" "$IMAGE" \
  --alias smriti-chat --host 0.0.0.0 --port 8080 --jinja --metrics $server_args >/dev/null
for _ in $(seq 1 120); do
  curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null && break
  docker ps -q -f name=bench-llm | grep -q . || { docker logs bench-llm 2>&1 | tail -20; exit 1; }
  sleep 2
done
vram=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null || echo "?")
echo "== $name  (VRAM in use: $vram)  $server_args"
mkdir -p bench-results
"$PY" -m smriti modelbench chat --target "http://127.0.0.1:${PORT}/v1,smriti-chat,${name}" \
  --extra '{"chat_template_kwargs":{"enable_thinking":false}}' \
  --json "bench-results/chat-${name}.json" "$@" | tail -4
docker rm -f bench-llm >/dev/null
