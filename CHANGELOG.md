# Changelog

## [Unreleased]

### Added
- **Built-in skills for every tenant:**
  - `pii-scan` counts personal data by category and flags public documents or unmasked categories. It never shows values.
  - `compare-documents` gives a paragraph redline between two documents.
  - `deadline-tracker` lists dates and "within N days" obligations, with due dates and a CSV.
  - `rti-reply` drafts an RTI Act reply with the section 7(1) deadline, flags possible section 8(1) exemptions, and never quotes a passage flagged for injection.
  - `citation-check` verifies pasted text sentence by sentence.
  - A pack overrides a built-in skill by reusing its name.
- **Agent inventory:** 17 templates in six purpose groups, served by the API (`agent_templates`). The web app's hard-coded list is gone.
- **Air-gapped install:** `deploy/offline/make-bundle.sh` and `install-offline.sh` package images, the model cache, the source and settings with checksums, and start Smriti with no network.
- **`smriti modelbench`:** benchmarks the model servers directly. `chat` sweeps concurrency and reports TTFT, TPOT, inter-token latency, tokens/s and goodput against a latency target, for one or more LLMs. `embed` and `rerank` report throughput by batch and candidate count. `retrieval` re-indexes the pack with every embedder and reranker pair and scores the golden set. Guide: `docs/model-benchmarks.md`.
- **llama.cpp for 6 GB GPUs:** a new `llm-gguf` service (llama.cpp CUDA, GGUF weights) answers on `llm:8000` like vLLM. The `gpu-6gb` profile now uses it with Qwen3-4B Q4_K_M, an 8-bit KV cache, 1,000 tokens of context and 4 concurrent answers, chosen from measurements on an RTX 4050 6 GB (`docs/model-benchmarks.md`). Both installers download the pinned, checksummed weights into the model cache, so the offline bundle carries them. vLLM stays available on that profile through `COMPOSE_PROFILES=llm,...`.
- **deploy/bench/:** `get-gguf.sh` (pinned GGUF downloads), `llm-sweep.sh` (one llama.cpp configuration per run) and a README for benchmarking vLLM on 12 GB and larger cards.
- **`modelbench chat` calibrates prompt size** against the server's own token count (the demo corpus is about 0.43 words per token, not 0.75) and reports the server's error message when a request fails.
- **docs/offline-stack.md:** library choices per pipeline stage for a zero-internet install (adopt, trial, assess or avoid, with licences).

## 0.2.0: safe and measured (Gate 0, security and data integrity)

This release fixes the bugs from the 3 November 2026 audit (`docs/roadmap.md`, section 3.6) and the Gate 0 security items (section 7.1). Every fix has a regression test in `tests/test_audit_fixes.py`.

### Changes you will notice

| Before | Now | What to do |
|---|---|---|
| Demo auth (`SMRITI_AUTH=demo`) served on any address | Refused unless the port is published on loopback | Use `SMRITI_AUTH=token` (or an SSO proxy) to serve a network. `SMRITI_DEMO_INSECURE=1` overrides for a throwaway demo |
| A request with no user acted as the first directory person | 401; only `/api/whoami` picks a person for a first visit in demo mode | Clients send `X-Smriti-User` (demo) or a bearer token |
| A widget without signed-in users could show internal material | Fixed-identity widgets are public-only; widgets receive only the verified final answer, class-limited and PII-masked | Set `max_class: public`, or switch the widget to signed users |
| Connected tools (REST, MCP, SQL) usable by any agent | Denied by default: the roles the tool lists, curators, or agents a curator published | List `allowed_roles` on each tool |
| Code execution available to any agent | Needs the `execute_code` grant; the sandbox service needs `SMRITI_SANDBOX_TOKEN` | Re-run the installer (it writes the token), or add the token to `.env` |
| `/api/metrics`, `/api/health?deep=1`, `/api/sources`, `/api/policy` open | Metrics and deep health need `SMRITI_METRICS_TOKEN` or the `audit`/`administer` grant; sources, policy and presets need `curate`, `audit` or `administer` | Give Prometheus a bearer token |
| Text and front matter in a crawled page or upload could set its own id, access and supersession | Only tenant packs may; supersession found in uploads and connector content is *proposed* for a curator | Review proposals at `GET /api/relations`; set a connector's "apply supersession" option for trusted sources |
| Jobs ended `done` even when pages failed | `partial` when any request or item failed; removal detection is skipped then | Re-run after the outage |

### Data loss and overwrite
- An upload whose name matches another person's document becomes a new document instead of a new version (#1).
- Access on a new version only tightens unless a curator uploads it.
- Front matter from non-pack sources can no longer set `doc_id`, `access` or relations. Text-derived supersession from those sources is proposed, not applied (#2).
- A site outage, failed items or an empty listing never tombstone documents. Removal needs a clean run and N consecutive misses (default 3) (#3).
- Re-uploading old text or a deleted file is no longer reported as a duplicate. Only the live, current version of a document counts (#13).
- New: `DELETE /api/documents/{id}` (tombstone) and `GET`/`POST /api/relations` (review proposed supersessions).

### Access leaks
- An unknown or misspelled clearance (for example `"Secret"`) grants nothing instead of everything. Classes compare case-insensitively (#8).
- Widgets: see the table above (#6).
- API tokens can be bound to a tenant (`token:tenant:user`) (#7).
- The pack-source crawler goes through the egress guard and has a download size cap. A `robots.txt` 5xx or 429 means "disallow for now", following RFC 9309 (#9).
- `/documents/{id}/access` answers a hidden document exactly like a missing one. An unknown user gives 404, not 500.

### Retry-safe ingest
- Embedding happens before any database write, so a down embedder leaves nothing behind (#4).
- If the vector store fails after the write, the document is marked `index_pending`. A retry or the scheduler's reconcile re-indexes it (#4).
- Failed downloads from pack sources stay out of the snapshot and are retried on the next run (#12).

### Integrity and guardrails
- Audit records are appended in one transaction under a per-tenant Postgres advisory lock, so several workers no longer fork the chain. `/api/stats` counts records instead of re-verifying the chain (#5).
- Prompt fields (title, section, reference, URL) are escaped and passage bodies cannot close their tag. Chat history passes the input guard and PII masking (#10).
- The malware scan fails closed when clamd is configured but cannot vouch for a file (`SMRITI_MALWARE_FAIL_OPEN=1` to override) (#11).
- The verifier checks negation ("not liable" never supports "liable"), including in Hindi. Devanagari digits are normalised. The answer cache never reuses an answer across a negated question (#14).

### Agents
- Prompt-injected evidence taints a run. A tainted run asks before any outbound tool, whatever the approval setting.
- Approvals over API keys are refused.
- An approval is used exactly once, even when two arrive together.
- The PII restore map of a paused run is sealed at rest and dropped when the run ends.
- Sandbox: mandatory token compared in constant time; child processes are killed with their process group; local mode only for administrators.
- Workflow expressions have a step budget and a result-size cap, and patterns with nested repetition are refused.

### Smaller fixes
- These no longer cause a 500: a corrupt PDF, a Hindi download filename, a NUL byte in a question, an empty model reply, an unknown user in the simulator.
- Uploads are read with a size cap.
- Hindi text files over 4 KB are recognised.
- Long text without sentence breaks is no longer duplicated.
- Dates and "30 days" are no longer read as clause numbers.
- A leading `---` rule no longer swallows a Markdown file's first section.
- Text hidden by a parent element is flagged.
- Semicolon CSVs are parsed.
- Empty or JavaScript-only pages ingest with a warning.
- Cron accepts day and month names, and a bad expression no longer stops the scheduler.
- `Thread._stop` is no longer shadowed, which had broken `join()` on Python 3.12.
- Curators can only approve memories from documents they can read, and erasing a memory deletes it.
- SQL row ids are stable across processes.
- Amendments match clause numbers exactly.
- "How many days…" is a lookup, not a table sum.

### Supply chain and headers
- Saaf is pinned to a commit.
- Third-party dependencies install from `requirements.lock` with `--require-hashes`.
- The base image and default service images are pinned by digest, including a dated SearxNG tag.
- Every page gets a CSP (`script-src 'self'`), `nosniff`, `Referrer-Policy` and `X-Frame-Options`. Inline event handlers are removed.
- Demo mode shows a banner.
