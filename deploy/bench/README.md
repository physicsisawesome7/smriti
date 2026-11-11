# LLM benchmarks on your own GPU

Measure an LLM server before putting Smriti in front of it: time to first
token, time per token, tokens per second and goodput (requests per second
within your latency targets) across a concurrency sweep. Metrics and tuning
order: [docs/model-benchmarks.md](../../docs/model-benchmarks.md).

## Small GPUs (6-8 GB): llama.cpp

vLLM's image is about 20 GB unpacked and its memory planning leaves little KV
cache on 6 GB. llama.cpp with 4-bit GGUF weights and an 8-bit KV cache fits
in a ~4 GB image and ~4.6 GB of VRAM, and is what we measured on an RTX 4050
laptop (results in the guide).

```bash
deploy/bench/get-gguf.sh                   # Qwen3 4B Q4_K_M, 1.7B Q8_0, 0.6B Q8_0 (~5 GB, checksummed)

# one configuration per run; results in bench-results/chat-NAME.json
deploy/bench/llm-sweep.sh 4b-q8kv \
  "-m /models/gguf/Qwen3-4B-Q4_K_M.gguf -ngl 99 -fa on -ctk q8_0 -ctv q8_0 -np 8 -c 16384" \
  --concurrency 1,2,4,8 --prompt-tokens 1500 --max-tokens 256
```

To serve Smriti with it, set `COMPOSE_PROFILES=llm-gguf,cpu-encoders` in
`.env` (instead of `llm,...`) and `docker compose up -d`. It answers on
`llm:8000`, so `SMRITI_CHAT_URL` does not change. Knobs: `GGUF_MODEL`,
`GGUF_KV_TYPE` (q8_0 or f16), `GGUF_CTX` (total tokens across all slots),
`LLM_MAX_NUM_SEQS` (slots) and `GGUF_EXTRA_ARGS`.

## 12 GB and larger: vLLM

Start the stack with the profile the installer picked (`./install.sh`), then
benchmark the `llm` service from inside the Compose network. Rebuild the image
first so it contains `smriti modelbench`:

```bash
docker compose build smriti-api
docker compose run --rm --no-deps --entrypoint smriti smriti-api modelbench chat \
  --target http://llm:8000/v1,smriti-chat,qwen3-4b-awq \
  --concurrency 1,2,4,8,16,32 --prompt-tokens 1500 --max-tokens 256 \
  --extra '{"chat_template_kwargs":{"enable_thinking":false}}' \
  --json /data/chat-vllm-12gb.json
mkdir -p bench-results
docker compose run --rm --no-deps --entrypoint cat smriti-api /data/chat-vllm-12gb.json > bench-results/chat-vllm-12gb.json
```

Then change one `.env` setting at a time, `docker compose up -d llm`, and run
the same command again:

| Try | Setting |
|---|---|
| Larger model, fewer users | `LLM_MODEL=Qwen/Qwen3-8B-AWQ`, `LLM_MAX_NUM_SEQS=4`, `LLM_GPU_MEMORY_UTILIZATION=0.62`, `LLM_MAX_MODEL_LEN=6144` |
| More users | raise `LLM_MAX_NUM_SEQS` while TPOT p95 holds |
| Prefill stalls (ITL p95 spikes) | lower `LLM_MAX_NUM_BATCHED_TOKENS` |
| Faster single-user decode | `LLM_SPECULATIVE=ngram` |
| More KV cache (Ada and newer only) | `LLM_KV_CACHE_DTYPE=fp8` |

Record the knee (`max conc in SLO`) per setting, set `SMRITI_CHAT_CONCURRENCY`
to it, and run `smriti eval` to make sure quality did not drop.

## Reading the result

The sweep prints, per concurrency level, tokens/s, TTFT p50/p95, TPOT, ITL p95
and the share of requests within the targets (`--slo-ttft-ms`, default 2000;
`--slo-tpot-ms`, default 100). The summary gives single-stream speed, peak
throughput and the highest concurrency that kept 90% of requests within both
targets. Prompts are calibrated against the server's own token count, so
`--prompt-tokens 1500` means about 1,500 tokens for that model's tokenizer.
