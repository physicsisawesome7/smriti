#!/usr/bin/env bash
# Builds the `vllm serve` command line from .env so the hardware profile, not
# this file, decides model, memory split and batching. Every flag here is a
# throughput or memory lever; see deploy/README.md for how each was chosen.
set -euo pipefail

args=(
  serve "${LLM_MODEL:?set LLM_MODEL in .env}"
  --host 0.0.0.0 --port 8000
  --served-model-name "${LLM_SERVED_NAME:-smriti-chat}"
  # memory: weights + KV cache must leave room for the embedder and reranker on the same GPU
  --gpu-memory-utilization "${LLM_GPU_MEMORY_UTILIZATION:-0.62}"
  --max-model-len "${LLM_MAX_MODEL_LEN:-8192}"
  # throughput: continuous batching with chunked prefill
  --max-num-seqs "${LLM_MAX_NUM_SEQS:-16}"
  --max-num-batched-tokens "${LLM_MAX_NUM_BATCHED_TOKENS:-4096}"
  # the platform rules and tenant policy open every prompt; the prefix cache reuses their KV
  --enable-prefix-caching
  --kv-cache-dtype "${LLM_KV_CACHE_DTYPE:-auto}"
  --dtype "${LLM_DTYPE:-auto}"
  --trust-remote-code
)

if [[ -n "${LLM_QUANTIZATION:-}" ]]; then
  args+=(--quantization "$LLM_QUANTIZATION")
fi
if [[ "${LLM_ENFORCE_EAGER:-0}" == "1" ]]; then
  # skips CUDA-graph capture: ~0.5-1 GB more for the KV cache on small cards, at some decode speed
  args+=(--enforce-eager)
fi
if [[ "${LLM_SPECULATIVE:-none}" == "ngram" ]]; then
  # prompt-lookup decoding: answers quote the retrieved passages, so drafts are often accepted.
  # No draft model, no extra VRAM.
  args+=(--speculative-config "{\"method\":\"ngram\",\"num_speculative_tokens\":${LLM_SPEC_TOKENS:-4},\"prompt_lookup_max\":4,\"prompt_lookup_min\":2}")
fi
if [[ -n "${LLM_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  args+=(${LLM_EXTRA_ARGS})
fi

echo "smriti: vllm ${args[*]}"
exec vllm "${args[@]}"
