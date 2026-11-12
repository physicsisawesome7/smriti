# Model benchmarks and throughput tuning

`smriti bench` load-tests the whole Smriti API. `smriti modelbench` measures the
model servers underneath it, so you can choose models and tune the servers
before Smriti sits in front of them. It only calls the URLs you pass and needs
no internet.

| Mode | Measures | Use it to decide |
|---|---|---|
| `chat` | TTFT, TPOT, ITL, end-to-end latency, tokens/s, goodput, across a concurrency sweep | which LLM, quantization and vLLM settings; how many people one GPU serves |
| `embed` | texts/s, tokens/s and batch latency by batch size | which embedder; how long a re-index takes |
| `rerank` | pairs/s and query latency by candidate count | how many candidates you can afford to rerank |
| `retrieval` | recall@10, MRR, citation precision, abstention and leaks on the golden set, for every embedder x reranker pair | which retrieval combination to ship |

## Terms

* **TTFT** (time to first token): prefill time plus queueing. Set by prompt length and load.
* **TPOT** (time per output token): (end-to-end - TTFT) / (output tokens - 1). 100 ms is about 10 tokens/s, faster than people read.
* **ITL** (inter-token latency): the gap between streamed tokens. Its p95 shows stalls when long prefills are scheduled next to your decode.
* **Goodput**: requests per second that met *both* the TTFT and the TPOT target. Throughput that misses the target is not useful throughput (DistServe, OSDI 2024).
* **Knee**: the highest concurrency that still meets the targets for 90% of requests. Size `SMRITI_CHAT_CONCURRENCY` and `LLM_MAX_NUM_SEQS` from it.

## Running it

From the host, against servers that publish a port:

```bash
smriti modelbench chat \
  --target http://127.0.0.1:8001/v1,smriti-chat,qwen3-4b-awq \
  --concurrency 1,2,4,8,16,32 --prompt-tokens 1500 --max-tokens 256 \
  --slo-ttft-ms 2000 --slo-tpot-ms 100 \
  --extra '{"chat_template_kwargs":{"enable_thinking":false}}' \
  --json bench-results/chat-qwen3-4b.json
```

Inside the Compose network, where the model services are not published, use the
Smriti image itself:

```bash
docker compose run --rm --entrypoint smriti smriti-api modelbench embed \
  --target http://embed:80/v1,Qwen/Qwen3-Embedding-0.6B --batch 1,8,32
```

To compare LLMs, start each one as its own vLLM container on a different port
(or one after another on the same GPU) and pass one `--target` per model.
Compare them at the same `--prompt-tokens`, `--max-tokens` and SLO, and run the
golden set on each (`smriti eval`) before deciding: a faster model that fails
the golden set is not a candidate.

Prompts are built from the demo pack's real documents, so they tokenize like
production prompts. `--shared-prefix` sends the same passages every time (the
best case for prefix caching); without it, a nonce at the start makes every
prompt unique (the worst case). Run both: the gap is what prefix caching buys.

## What to tune, in order

Change one thing, rerun the same command, keep the JSON. Every change must
also pass the golden set, red team and leakage suite (`smriti eval`).

### 1. Don't call the model

The cheapest token is one you never generate. Before touching vLLM, check
`smriti bench` for `llm_share`: the answer cache, the extractive writer for
lookup questions and abstention already answer part of the traffic without the
LLM.

### 2. Prompt size (TTFT)

| Knob | Effect |
|---|---|
| `SMRITI_CONTEXT_TOKENS` | Passages sent to the model. Prefill cost grows with it. 1,200 to 1,800 is enough when the reranker is good |
| Fewer, better passages | A cross-encoder reranker lets you send 5 passages instead of 15 at the same recall |
| `--enable-prefix-caching` | On by default. The platform rules and tenant policy open every prompt, so their KV cache is reused |

### 3. Batching (throughput vs tail latency)

| Knob | Raise it when | Lower it when |
|---|---|---|
| `LLM_MAX_NUM_SEQS` | the GPU is under-used at the knee and TPOT has headroom | TPOT p95 breaks the SLO |
| `LLM_MAX_NUM_BATCHED_TOKENS` | TTFT is high for long prompts (more prefill per step) | ITL p95 spikes: long prefills are stalling decodes |
| `SMRITI_CHAT_CONCURRENCY` | set it to the knee | requests queue in Smriti instead of in vLLM |

Chunked prefill (on in vLLM V1) splits long prompts across steps, so one 8k-token
prompt does not freeze every other stream.

### 4. Memory (how many sequences fit)

| Knob | Trade-off |
|---|---|
| Weight quantization (`LLM_QUANTIZATION=awq_marlin`, GPTQ, FP8 on Ada/Hopper) | 4-bit weights roughly halve memory against FP16 and speed up memory-bound decode. Check the golden set: small models lose more |
| `LLM_KV_CACHE_DTYPE=fp8` | About twice the KV cache, so about twice the concurrent sequences. Used on Ada and newer; kept off on Ampere |
| `LLM_GPU_MEMORY_UTILIZATION` | More KV cache, less room for the encoders on the same GPU |
| `LLM_MAX_MODEL_LEN` | Lower caps the worst-case sequence and frees KV blocks |
| `LLM_ENFORCE_EAGER=1` | Frees 0.5 to 1 GB on 6 GB cards but slows decode. Only on the smallest profile |

### 5. Decode fewer times

`LLM_SPECULATIVE=ngram` turns on prompt-lookup speculative decoding. RAG answers
quote their passages, so drafts are often accepted; it needs no draft model and
no extra memory. Measure it with `--shared-prefix` off: the gain varies by
workload and can be negative at high concurrency, where the GPU is no longer
memory-bound.

### 6. Encoders

* **Embedder**: batch size matters more than anything else on CPU. Re-index
  time is `chunks / texts_per_s`; the `embed` mode prints the hours for a
  million chunks.
* **Reranker**: latency grows linearly with candidates. Rerank 30 to 50, not
  200; if recall needs more, fix first-stage retrieval (hybrid search,
  contextual chunk headers) rather than reranking deeper.
* Put the encoders on the GPU (`gpu-encoders`) once you have more than one
  concurrent user; on CPU a 0.6B embedder is fine for queries but slow for
  bulk ingestion.

## Choosing models

`modelbench retrieval` indexes the tenant pack from scratch with each embedder
and reranker and scores the golden set with the LLM switched off, so the score
is retrieval only:

```bash
smriti modelbench retrieval \
  --embed hash \
  --embed http://embed:80/v1,intfloat/multilingual-e5-small,"query: ","passage: " \
  --embed http://embed-qwen:80/v1,Qwen/Qwen3-Embedding-0.6B \
  --rerank none --rerank http://rerank:80,BAAI/bge-reranker-v2-m3 \
  --json bench-results/retrieval.json
```

Public leaderboards (MTEB) are a shortlist, not a decision: your documents,
languages and question styles decide. A small multilingual model with a good
reranker often matches a large embedder alone, at a fraction of the memory.

## Results on the reference machine

Laptop: AMD Ryzen 7 7435HS (16 threads), Docker with 12 GB, encoders on CPU
(TEI `cpu-1.9`), the demo pack (14 documents, 39 chunks). 11 November 2026.
Raw JSON goes to `bench-results/` (ignored by git).

**Embedding throughput** (`modelbench embed`, 350-token texts, concurrency 2):

| Embedder | Batch 1 | Batch 8 | Batch 32 | 1M chunks, best |
|---|---|---|---|---|
| multilingual-e5-small | 3,714 tok/s | 5,068 tok/s | 5,552 tok/s | ~24 h |
| Qwen3-Embedding-0.6B | 122 tok/s | 72 tok/s | 66 tok/s | ~56 days |

Qwen3-Embedding on TEI's CPU backend is 30-85x slower and gets slower with
larger batches. Keep it on GPU profiles; e5-small is the CPU default.

**Golden set** (`modelbench retrieval`, hybrid, built-in reranker): hash,
e5-small and Qwen3-0.6B all pass 14/14 with recall@10 = MRR = 1.0 and 0 leaks.
Indexing took 0.7 s, 1.7 s and 49.5 s. The golden questions share the
documents' wording, so BM25 and the contextual headers find them alone: the
set is saturated and cannot choose an embedder. Add paraphrased and
cross-language questions to your pack's golden set.

**Hard questions** (`scripts/hard_query_probe.py`: 10 paraphrased or
cross-language questions, Smriti-style fusion, top 20 per list, RRF k=60):

| Setup | Top 1 | Top 5 | MRR | Cross-language top 5 |
|---|---|---|---|---|
| BM25 only | 4 | 5 | 0.47 | 0/4 |
| e5-small dense | 9 | 10 | 0.93 | 4/4 |
| e5-small + BM25 (RRF) | 7 | 9 | 0.81 | 3/4 |
| Qwen3-0.6B dense | 7 | 10 | 0.79 | 4/4 |
| Qwen3-0.6B + BM25 (RRF) | 6 | 8 | 0.73 | 2/4 |

Equal-weight fusion costs quality when the question does not share the
document's words; BM25 is what makes the keyword-heavy golden set pass.
Open item: a cross-encoder after fusion, weighted fusion or skipping BM25 for
cross-script questions. Small sample; treat as direction, not a ranking.

**LLM on a 6 GB laptop GPU** (`deploy/bench/llm-sweep.sh`): RTX 4050 Laptop
6 GB, llama.cpp CUDA build 12282, all layers on the GPU, flash attention, 8
slots. Prompts are calibrated to about 1,450 tokens of the demo corpus (or
1,000 where noted), answers up to 256 tokens, thinking off. Targets: first
token within 2 s, under 100 ms per token after that. "Users in target" is the
highest concurrency where at least 90% of requests met both.

| Configuration | VRAM | 1 user | 2 users | 4 users | 8 users | Users in target |
|---|---|---|---|---|---|---|
| Qwen3-4B Q4_K_M, q8_0 KV cache | 4.6 GB | 53 tok/s, TTFT 0.7 s | 76 tok/s | 57 tok/s | 104 tok/s | 2 |
| same, f16 KV cache | 5.6 GB | 57 tok/s | 56 tok/s | 61 tok/s | 83 tok/s | 2 |
| same, `-ub 2048` prefill batches | 4.8 GB | 52 tok/s | 54 tok/s | 50 tok/s | 102 tok/s | 2 |
| q8_0 KV, 1,000-token prompts | 4.7 GB | 51 tok/s | 59 tok/s | 68 tok/s | 113 tok/s | 4 |
| q8_0 KV, shared prompt prefix | 4.7 GB | 54 tok/s, TTFT 27 ms | 90 tok/s | 140 tok/s | 163 tok/s | 8 |
| Qwen3-1.7B Q8_0, q8_0 KV | 3.9 GB | 76 tok/s, TTFT 0.3 s | 66 tok/s | 71 tok/s | 110 tok/s | 8 (0.96) |

"1 user" is the single-stream decode speed; the other columns are total output
tokens per second. VRAM includes about 0.7 GB held by the Windows desktop.

What this says about a 6 GB card:

* **Prefill, not decode, sets the limit.** With unique 1,450-token prompts,
  each new request's prefill (~0.7 s) stalls everyone else's decoding, so
  time per token triples at 4 users. With a shared prefix the same model
  serves 8 users within target. Smriti's prompts open with the same rules and
  policy, so real traffic sits between the two rows; keep that prefix stable.
* **Context is the cheapest lever.** 1,000 instead of 1,450 prompt tokens
  doubled the users within target (2 to 4). The 6 GB profile sets
  `SMRITI_CONTEXT_TOKENS=1000` and `SMRITI_CHAT_CONCURRENCY=4`.
* **The 8-bit KV cache wins on this card**: 25% more throughput at 8 streams
  and 1 GB less memory than f16. Larger prefill batches did not help.
* **A smaller model is faster for one person, not for many.** Qwen3-1.7B
  answers a single user 40% faster and starts in half the time, but plateaus
  at the same ~110 tokens/s under load. Check the golden set before trading
  the 4B for it.
* **Why llama.cpp, not vLLM, on 6 GB**: the vLLM image is ~20 GB unpacked and
  on 6 GB needs eager mode with ~1.5 GB of KV cache; llama.cpp ran in a ~4 GB
  image and 4.6 GB of VRAM. The `gpu-6gb` profile now uses it (`llm-gguf`).
  vLLM was not benchmarked on this machine (no disk space for its image), so
  this is a fit decision, not a measured race.

Not tested yet: speculative decoding (draft model or n-gram), and 12 GB+
cards. For those, see [deploy/bench/README.md](../deploy/bench/README.md).

### The whole stack on the 6 GB profile

Measured on 12 November 2026 with `./install.ps1 -Profile gpu-6gb` (llama.cpp,
Qwen3-4B Q4_K_M, encoders on the CPU, Postgres and Qdrant) and
`smriti bench --no-cache --requests 24 --metrics-user nadia`. Every question is
unique, so the answer cache cannot help. VRAM in use: 4.4 GB.

| Simultaneous users | Answers per second | p50 latency | p95 latency | Errors | Requests shed |
|---|---|---|---|---|---|
| 1 | 0.48 | 1.4 s | 6.9 s | 0 | 0 |
| 4 | 0.97 | 2.7 s | 15.0 s | 0 | 0 |
| 8 | 1.06 | 6.3 s | 16.8 s | 0 | 0 |

Of 24 questions, 16 were written by the model, 6 by the extractive writer, 2
were computed from table rows and 2 abstained. Model answers used about 1,540
prompt tokens and 125 to 185 completion tokens.

What this says:

- Throughput stops rising after 4 users (0.97 to 1.06 answers per second), which
  matches the knee of 4 measured on the model server alone. Above that, requests
  queue and the median wait more than doubles.
- The slow tail is the model: questions the extractive writer or the table
  calculator can answer return in well under a second.

Two things this run exposed, both still open:

- **Prompts are about 50% larger than the setting.** `SMRITI_CONTEXT_TOKENS=1000`
  produced 1,540-token prompts, because the budget is estimated from words and
  this corpus runs at about 0.43 words per token. It fits the 3,072-token slots
  of this profile, but the budget should be counted with the server's tokenizer.
- **Token meters are per process.** With two API workers, `/api/metrics.json`
  reports only the worker that answers the request, so the model-call and token
  counts above are undercounts at 4 and 8 users (the `answered_by` counts come
  from the answers themselves and are right).

