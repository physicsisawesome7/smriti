# Smriti: test report and plan for the next release

Date: 12 November 2026. This extends [roadmap.md](roadmap.md) (the November
audit) with what was tested today and what to build next. Goal: an on-premise,
production-grade RAG and agent platform that installs in one command, can be
configured for any use case without code, states its limits, and earns
attention with numbers anyone can reproduce.

## 1. What was tested today

| Check | Result |
|---|---|
| Unit and integration tests | 150 passed, 1 skipped (Qdrant client not installed in the dev venv) |
| Lint (ruff) | Clean |
| Release gate: golden set | 14/14, recall@10 1.0, MRR 1.0, citation precision 1.0, abstention 1.0 |
| Release gate: leakage | 102 probes, 0 violations |
| Release gate: red team | 6/6 blocked, attack success 0% |
| Audit chain | Verifies (35 records) |
| Pack validation (`railways-demo`) | Valid |
| CI on `9408058` | ci, docker and codeql green |
| Model benchmarks | Measured on the RTX 4050 6 GB and CPU ([model-benchmarks.md](model-benchmarks.md)) |
| Running Docker stack | Up 7 days, but see problems below |

Problems found:

1. **Worker and sandbox always report "unhealthy".** They inherit the image's
   health check, which probes port 8000; the worker has no HTTP port and the
   sandbox listens on 8080. Both work; the status is wrong. Anything that
   waits on health (orchestrators, the installer's status view) is misled.
2. **The running stack is the old 0.1.0 image**, built before the security
   release. It needs a rebuild before any further end-to-end test.
3. **The API stopped answering on the host port** after a week (it answers
   inside the container in 20 ms). This is Docker Desktop's port forwarding
   after the engine restart on 11 November, not Smriti, but the installer's
   `-Status` should detect it and say so.
4. **The golden set is saturated.** Every embedder, including the hashing one,
   scores 14/14 on 14 documents. It protects against regressions but cannot
   choose a model or prove an improvement.
5. **Equal-weight hybrid fusion hurts paraphrased and cross-language
   questions**: on the ten-question hard probe, dense alone got 9/10 at rank 1
   and hybrid got 7/10.

Not tested (unchanged from the README's limits): OCR, layout parsing and
speech-to-text (not bundled); Keycloak, OpenFGA and Temporal adapters; vLLM on
12 GB and larger cards; a live model driving connected tools; load on the full
stack; the `llm-gguf` Compose service started end to end.

## 2. Where Smriti stands

Ahead: permission checks at two points with a leakage suite, "as of" answers
over amendment chains, sentence-level verification with abstention, PII
masking by role, a hash-chained audit log, answers with no model, agents that
act as the asker, one-command install with GPU profiles, an offline bundle.

Behind what a 2026 production RAG system is expected to have:

| Area | Gap |
|---|---|
| Retrieval | Equal-weight RRF only; no query rewriting, no contextual chunk text for the embedder, no parent expansion, no diversity step, no learned sparse or late interaction |
| Evaluation | 14 golden questions; no per-slice scores; no way for a site to build its own test set from its documents |
| Configuration | One vertical (railways). A new use case means writing YAML by hand; no presets, no setup screen, no "test my configuration" button |
| Content | No bundled OCR or layout parser, so scanned PDFs and complex tables wait in a queue |
| Identity | Token or trusted header; no OIDC sign-in |
| Answers | No token streaming of verified sentences; the verifier ignores negation in places |
| Operations | Prometheus counters only; no traces, no per-user budgets, no backup command |
| Limits | Stated as prose in the README, not as tested numbers per profile |

## 3. Plan

Each phase ends with the release gate green and is committed separately.
Sizes are engineering effort for one person.

### Phase A: fix what the tests found (done 12 November 2026)

Done: health checks per service, installer status, the full stack rebuilt and
run on the 6 GB card with llama.cpp, a full-stack load test
([model-benchmarks.md](model-benchmarks.md)), and limit and scope checks in the
verifier. Running the stack found three more bugs, now fixed: the installer
never saw "ready" (it waited on a URL that needs sign-in), `smriti bench`
crashed on protected meters, and both were invisible to the unit tests.

Still open, moved to Phase C and E: the context budget is counted in words, so
prompts run about 50% over the setting; token meters are per process, so they
undercount with more than one API worker.

Original list:


- Health checks per service (worker: process check; sandbox: port 8080).
- Installer `-Status` / `--status` probes the API from the host and explains a
  dead port forward.
- Rebuild the stack, start `llm-gguf` end to end on the 6 GB card, run
  `smriti bench` against the full stack and record it.
- Verifier: keep negation and quantifiers (roadmap 7.4 items 1-4).

### Phase B: a test set that can tell systems apart (done 12 November 2026)

Done: the hard set (268 documents, 190 questions, eight slices),
`smriti eval hard`, `smriti eval compare` and `smriti eval build`; see
[evaluation.md](evaluation.md) for use and the baseline. It was built as a
generator instead of 300 hand-written files, so it is reproducible and can be
re-seeded.

The baseline (e5-small with Qwen3-4B, no reranker) passes 55% overall with
zero leaks, and re-orders Phase C by measured size of gap:

1. Paraphrases: the right document is retrieved for 66% but only 18% are
   answered, because Smriti abstains on a low support score before the model
   is asked. Fix the answering gate first (C1, C5).
2. Cross-language retrieval is 24% (C1, C2, C8).
3. Multi-hop is 0% (C6).
4. Totals over Markdown tables are not computed (new item for C).
5. A third of unanswerable questions are answered from a near-miss document
   (C6).

Original list:


Everything after this is judged by it, so it comes first.

- **Hard set v1**: about 150 questions over a larger synthetic corpus
  (about 300 documents, three domains), sliced as exact lookup, paraphrase,
  cross-language (Hindi, Hinglish), multi-hop, table arithmetic, superseded
  rule, unanswerable, and permission-restricted. Per-slice gates.
- **`smriti eval build`**: generates candidate questions from a site's own
  documents with the configured model, for a curator to accept or reject. This
  is how a site proves the system on its own data.
- **`smriti eval compare A B`**: two configurations, same questions, a table
  with confidence intervals. Used for every retrieval change below.

### Phase C: retrieval and answer quality (2-3 weeks)

Each item ships only if it improves the hard set without lowering any slice.

1. **Adaptive fusion**: weighted RRF with the weight chosen per query (exact
   identifiers and numbers lean lexical; paraphrase and cross-language lean
   dense). Fixes the 9/10 against 7/10 result.
2. **Query understanding**: abbreviation expansion from the pack glossary,
   follow-up resolution, translation of Hindi and Hinglish, and decomposition
   of multi-part questions. Rule-based without a model, model-assisted with
   one.
3. **Contextual chunk text**: prepend the deterministic header (document,
   section, date, status) to what the embedder sees, with a model-written
   one-line context as an option. Kept only if recall rises by 3 points.
4. **Parent expansion**: retrieve small chunks, hand the model the enclosing
   clause or table.
5. **Reranking that fits the hardware**: cross-encoder with a token budget and
   early exit; on CPU-only installs rerank the top 20, not 50.
6. **Corrective loop**: grade the retrieved set; if support is low, rewrite
   and retry once before abstaining. Bounded to one retry.
7. **Streaming verified sentences** so the first sentence appears in under a
   second on a GPU.
8. **Encoder choice measured**: BGE-M3 (dense and sparse from one model) and
   int8 vectors with rescoring, against the current default.

Deliberately not planned: a knowledge-graph index (cost and upkeep are high
for small sites), late-interaction storage, and fine-tuning. They stay in
"assess" until the hard set shows a slice the list above cannot fix.

### Phase D: configure for any use case, without code (2 weeks)

- **Use-case presets** as packs: policy and HR helpdesk, contracts and
  procurement, technical manuals and maintenance, customer support, research
  library, legal and compliance. Each sets chunking, retrieval weights, agent
  templates, guardrails, a starter test set and example documents.
- **Setup screen on first run**: pick a preset, name roles and access classes,
  add the first source, pick or detect the model profile, then a "Test this
  setup" step that runs the preset's questions and shows a pass or fail.
- **`smriti init <preset>`** for the same thing from the command line, and
  `smriti doctor` to check GPU, disk, memory, ports and model licences before
  and after install.
- **Settings in the UI** for the things people change most: models, retrieval
  depth, answer length, languages, masking rules.
- **Bundled OCR and layout parsing** as an optional profile (Hindi and
  English), so scanned letters and tables work on day one.
- **OIDC sign-in** (Keycloak, Entra, Google Workspace), with groups mapped to
  roles.

### Phase E: production hardening (2 weeks, overlaps D)

- Per-user and per-tenant rate limits and token budgets.
- OpenTelemetry traces for one question end to end; a Grafana dashboard file.
- `smriti backup` and `smriti restore` (database, blobs, index), tested.
- Upgrade path: migrations and a version check that refuses a downgrade.
- A 30-minute soak and a load test of the whole stack per profile, in CI
  nightly for the CPU profile.

### Phase F: website and launch (1 week, after B-D)

Website (veil repo, `apps/website`):

- A **limits page** generated from the tested numbers in section 4.
- A **use-cases page** with one card per preset and its test results.
- A **benchmarks page** that reads `bench-results/*.json`, so numbers on the
  site are never typed by hand.
- Quick start rewritten around `smriti init`; a 60-second demo recording.
- Case study updated with the hard set and the adaptive-fusion result.
- Docs for presets, evaluation and tuning.

Getting noticed (what is in our control):

- **A public trust benchmark**: permission leakage, citation correctness and
  abstention, run against Smriti and other open-source assistants with a
  script anyone can re-run. This is the one thing competitors do not publish.
- **A five-minute install that works on a laptop GPU**, shown in the README's
  first screen with a recording and measured numbers.
- Launch posts: Show HN, r/LocalLLaMA, r/selfhosted, Product Hunt, LinkedIn,
  and a technical write-up of the 6 GB results.
- Listings: awesome-selfhosted, awesome-rag, the MCP server registry.
- "Good first issue" labels, a public roadmap, and a release every two weeks.

Stars cannot be promised. The plan makes the project easy to try, easy to
verify and different in a way that is measurable.

## 4. Limits to publish

Measured numbers are marked; the rest are targets to confirm in Phase A and E
before they go on the website.

| Limit | CPU only | 6 GB GPU | 12 GB GPU | 24 GB GPU |
|---|---|---|---|---|
| Chat model | None (extractive answers) or remote | Qwen3-4B 4-bit (measured) | 4B-8B class | 14B-30B class |
| Simultaneous chat users within the latency target | Not applicable | 4 (measured; 8 with a shared prompt prefix) | To measure | To measure |
| First token | Not applicable | 0.3-2 s (measured) | To measure | To measure |
| Embedding speed, small encoder | About 5,000 tokens/s (measured) | Same (encoders on CPU) | GPU encoder: to measure | To measure |
| Time to index 1 million chunks | About 24 hours (measured) | About 24 hours | To measure | To measure |
| Recommended corpus | Up to 100,000 chunks | Up to 500,000 | Up to 2 million | Up to 10 million (Qdrant) |

Fixed limits, all profiles:

- One organisation per install (multi-tenancy is not on by default).
- Languages tested: English and Hindi. Others depend on the configured models.
- Files: the formats the built-in parsers read (the exact list is to be
  confirmed from `parsers.py` in Phase A and tested one by one). Scanned
  pages need the OCR profile. Audio and video are not supported.
- Archives: up to 2,000 entries, 1 GB unpacked, compression ratio up to 100.
  The per-file upload cap is to be confirmed in Phase A.
- Questions up to 4,000 characters; answers up to the model route's token cap
  (512 by default).
- Agents: 6 steps by default (configurable per agent), approval required after untrusted content, code runs
  only in the sandbox with no network.
- Not for: real-time data, unreviewed legal or medical decisions, or answers
  without sources. Smriti abstains when support is weak, by design.

## 5. Order and time

A (3 days), B (1 week), C (2-3 weeks), D (2 weeks) with E alongside, F (1
week). About 8 weeks for one person. The first public release candidate is
after B and the first two items of C and D (adaptive fusion, presets, setup
screen), about 4 weeks in.
