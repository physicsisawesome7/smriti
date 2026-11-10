# Deploying Smriti on-premise

One command brings up the whole stack on one machine with one consumer GPU:

```bash
./install.sh            # Linux, WSL2, macOS
```
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1    # Windows (Docker Desktop, WSL2 backend)
```

The installer checks Docker and GPU access inside containers, picks a hardware profile from your VRAM, writes `.env`, builds and starts everything, and waits until every model server is ready. Re-running it keeps your `.env`.

## What runs

```
                        ┌──────────── smriti-api (N processes) ────────────┐
 browser / OpenAI SDK ─►│ guard ▸ cache ▸ retrieve ▸ rerank ▸ write ▸ verify│
 MCP clients            └───┬──────────┬───────────┬───────────┬───────────┘
                            │          │           │           │
                     postgres     qdrant      embed+rerank    llm (vLLM)
                     (docs, audit) (int8 vecs)  (TEI)         (AWQ, prefix cache)
                            ▲
 smriti-worker ─────────────┘  runs ingestion jobs (uploads, websites, APIs, databases, MCP),
                               crawls sources on schedule, raises alerts
```

## Profiles

| Profile | Card | LLM | Encoders | KV cache | In flight |
|---|---|---|---|---|---|
| `cpu` | none | none: verified extractive answers | CPU (e5-small, ONNX) | — | — |
| `gpu-6gb` | RTX 4050/3050 laptop | Qwen3-4B AWQ, eager | CPU (e5-small, ONNX) | fp16, ~10k tok | 4 |
| `gpu-12gb` | **RTX 3060 12 GB** | Qwen3-4B AWQ | GPU fp16 | fp16, ~22k tok | 16 |
| `gpu-16gb` | **RTX 5070 Ti 16 GB** | Qwen3-8B AWQ | GPU fp16 | fp8, ~53k tok | 16 |
| `gpu-24gb` | RTX 3090/4090 | Qwen3-14B AWQ | GPU fp16 | fp8, ~80k tok | 32 |

Each profile file (`deploy/profiles/*.env`) shows its VRAM arithmetic. Override any value in `.env`. Force a profile with `./install.sh --profile gpu-12gb --reconfigure`.

All default models are Apache-2.0: Qwen3 AWQ checkpoints published by Qwen, Qwen3-Embedding-0.6B and BAAI bge-reranker-v2-m3 (multilingual, Hindi included).

## Why it is cheap to run

Most of the saving is in not calling the GPU, or sending it fewer tokens:

1. **Extractive-first.** Clause lookups and table questions are answered by a verified quote or by maths over source rows. The LLM is not called. On the demo golden set, 7 of 14 questions never reach the LLM: lookups, table maths, blocked attacks and abstentions.
2. **Guarded answer cache.** Exact and near-duplicate questions (cosine ≥ 0.94) from people with identical access reuse the verified answer. Questions with different numbers or reference ids never share an answer.
3. **Context compression.** Passages are cut to their relevant sentences so the evidence never exceeds `SMRITI_CONTEXT_TOKENS` (1800 by default), however long the source documents are. Verification still checks against the full text. The demo corpus has short chunks, so its prompts (~1.3k tokens) already sit under the cap; the saving shows on long manuals and codes.
4. **Prefix caching.** The prompt opens with the platform rules and tenant policy, identical for every request, so vLLM reuses their KV cache instead of recomputing it.
5. **No reasoning tokens.** Qwen3's thinking mode is turned off per request (`SMRITI_CHAT_EXTRA`). Any `<think>` output is stripped.
6. **AWQ 4-bit weights via Marlin kernels** on Ampere and newer. **FP8 KV cache** on Ada and Blackwell doubles concurrent context.
7. **Admission control.** Each API process lets at most `SMRITI_CHAT_CONCURRENCY` requests reach vLLM. Excess requests wait up to 20 s, then get the verified extractive answer (`load_shed` in the trace) instead of queueing on the GPU until they time out.
8. **Optional prompt-lookup speculative decoding** (`LLM_SPECULATIVE=ngram`). Answers copy text from the passages, so draft tokens are often accepted, with no draft model and no extra VRAM. Benchmark it on your card before turning it on.

## Measure it

```bash
docker compose exec smriti-api smriti bench --concurrency 8 --requests 200
docker compose exec smriti-api smriti bench --concurrency 8 --requests 200 --no-cache   # GPU path only
```

The bench reports p50/p95/p99 latency, answers per second, the share of answers that needed the LLM, prompt and completion tokens per LLM call, GPU tokens per answer, cache hits and load shedding. Prometheus metrics are at `/api/metrics`, readiness at `/api/health?deep=1`.

Measured with the `cpu` profile in Docker Desktop on a 16-core laptop (no LLM, Postgres + Qdrant + TEI e5-small), 120 requests at concurrency 8:

| | answers/s | p50 | p95 |
|---|---|---|---|
| every question unique (`--no-cache`) | 46 | 160 ms | 251 ms |
| realistic repeats (cache on, 88 of 120 hits) | 103 | 57 ms | 172 ms |

The release gate (14/14 golden, 0 leaks in 102 probes, 6/6 red team) passes on the containerised stack.

The GPU profiles add LLM generation time on top. Measure them on your card with `smriti bench --no-cache`.

## Customise

| Change | How |
|---|---|
| Different model | `LLM_MODEL=...` (any vLLM-supported AWQ or GPTQ checkpoint), then `docker compose up -d llm` |
| Longer context | raise `LLM_MAX_MODEL_LEN`, lower `LLM_MAX_NUM_SEQS` to keep the KV budget |
| LLM on another machine | `SMRITI_CHAT_URL=http://host:8000/v1`, `SMRITI_CHAT_MODEL=...`, and remove `llm` from `COMPOSE_PROFILES` |
| Ollama or llama.cpp instead of vLLM | point `SMRITI_CHAT_URL` at its OpenAI-compatible endpoint |
| Ingest a shared drive | put or mount it under `./imports` (or set `SMRITI_IMPORTS_DIR`), then add a *Server folder* source for `/imports/...` |
| Crawl intranet hosts only | `SMRITI_EGRESS_ALLOW_PRIVATE=1` (default). Add exceptions with `SMRITI_EGRESS_ALLOWLIST=host,10.0.0.0/8` |
| Your own documents and roles | copy `packs/tenants/railways-demo` to `packs/tenants/<you>`, edit the YAML, set `SMRITI_PACKS=/app/packs/tenants/<you>` |
| Serve the LAN | `SMRITI_BIND=0.0.0.0`, `SMRITI_AUTH=token` (demo auth is refused off loopback), and a TLS reverse proxy in front |
| Real logins | `SMRITI_AUTH=token` with `SMRITI_API_TOKENS=token:user` or `token:tenant:user`, behind your SSO proxy |
| Prometheus | `SMRITI_METRICS_TOKEN=...`, then scrape `/api/metrics` with that bearer token |
| Malware scanning | `SMRITI_CLAMD=host:3310`; a scan that cannot vouch for a file rejects it (`SMRITI_MALWARE_FAIL_OPEN=1` to override) |
| More API processes | `SMRITI_WORKERS=4` (stateless; Qdrant and Postgres are shared) |
| Web search for agents | add `search` to `COMPOSE_PROFILES` and set `SMRITI_SEARXNG_URL=http://searxng:8080` (self-hosted SearxNG) |
| Agents' Python sandbox | runs by default on an internal-only network. Tune `SANDBOX_MEMORY` and `SANDBOX_CPUS`; set `SMRITI_SANDBOX_URL=` (empty) to turn code execution off |
| Tool calling off | `LLM_TOOL_PARSER=none` (agentic agents then use the rule planner) |

Packs are mounted read-only from `./packs`. After editing them, run `docker compose restart smriti-api smriti-worker`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no kernel image is available` from vLLM on an RTX 50-series card | the image lacks sm_120 kernels. Set `VLLM_IMAGE` to NVIDIA's build (`nvcr.io/nvidia/vllm:<yy.mm>-py3`) or a newer `vllm/vllm-openai` tag |
| vLLM: `not enough free memory` at start | another process holds VRAM (browser, games, another model). Lower `LLM_GPU_MEMORY_UTILIZATION` by 0.05 |
| vLLM out of memory under load | lower `LLM_MAX_NUM_SEQS` or `LLM_MAX_MODEL_LEN`, or set `LLM_ENFORCE_EAGER=1` |
| TEI fails on your GPU | set `TEI_IMAGE` to the tag for your architecture (see the TEI README), or move encoders to CPU: `COMPOSE_PROFILES=llm,cpu-encoders,cpu-rerank` |
| Containers can't see the GPU | Linux: install the NVIDIA Container Toolkit. Windows: update the NVIDIA driver and Docker Desktop (WSL2 backend) |
| Air-gapped site | run `./install.sh` once online with the site's profile, then `deploy/offline/make-bundle.sh`; on the site run `./install-offline.sh` from the bundle (checksums, `docker load`, model cache restore, `compose up --pull never`). See [docs/offline-stack.md](../docs/offline-stack.md) |
