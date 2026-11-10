# Offline stack: libraries, skills and agents for a zero-internet install

Status: plan and inventory, November 2026. It belongs with [roadmap.md](roadmap.md), Phase 1 (sovereign pilot kit).

**Goal:** one command installs Smriti on a machine that never reaches the internet. The install scales from one GPU box to a small cluster. Every part of the RAG pipeline runs locally.

This page covers:

- **Section 1:** the rules every library must pass.
- **Section 2:** each pipeline stage, what Smriti uses today, and what to adopt, trial or avoid.
- **Section 3:** how the one-click air-gapped install works.
- **Section 4:** the skills and agents that now ship in the box.

## 1. Rules for anything we add

A library or service is accepted only if it passes all six rules:

1. **No network at runtime.**
   - No calls home, licence checks, telemetry or model downloads on first use.
   - Telemetry that is on by default must be switched off in our images, not left to the operator's documentation.
2. **Weights and data can be fetched ahead of time.**
   - Everything a library downloads lazily (OCR models, tokenizers, NLTK data, layout models) must be fetched into the bundle at build time.
   - It must be found there with `HF_HUB_OFFLINE=1`.
3. **The licence allows us to ship it inside a product.**
   - Apache-2.0, MIT, BSD and PostgreSQL are fine.
   - AGPL and GPL are acceptable only as a separate service we don't link to (SearxNG is one).
   - "Non-commercial" model weights are never acceptable (several Jina models fall in this group).
4. **It can be pinned.** Every package needs a hash in `requirements.lock` and every image a digest.
5. **It runs on the hardware profiles we sell:** CPU only, and 6, 12, 16 and 24 GB GPUs.
6. **It doesn't replace our trust core.** Access control, version rules, the verifier, PII masking and the audit log stay in Smriti's own code. Libraries feed that core; they are never the judge.

Verdicts used below:

| Verdict | Meaning |
|---|---|
| **Adopt** | Ship it in the default install. |
| **Trial** | An optional extra or profile; measure it on the gates first. |
| **Assess** | Watch it, and spike it only if a pilot asks. |
| **Avoid** | Fails a rule above. |

## 2. Stage by stage

The stages follow the common RAG pipeline picture: sources, parsing, embeddings, vector store, retrieval, generation, evals and deployment. We add two stages it leaves out: the agent harness, and OCR for Indian scripts. Cloud services in that picture (OpenAI, Cohere and Voyage embeddings, Pinecone, Bedrock, Modal, Cloudflare) fail rule 1 and are listed as Avoid only once, here.

### 2.1 Data sources, loaders and web scrapers

**Today:**
- An own crawler built on `httpx` and BeautifulSoup, with optional Playwright for JavaScript-heavy sites.
- Connectors for REST, SQL (PostgreSQL and MySQL), MCP, S3, feeds, email and server folders.
- Every request passes the egress guard.

| Library | Licence | Verdict | Why |
|---|---|---|---|
| **Trafilatura** | Apache-2.0 | **Adopt** | Main-content extraction from HTML: it drops menus, footers and cookie banners. It is pure Python with no model, and is the biggest quality win for intranet crawls. |
| **selectolax** | MIT | Trial | An HTML parser 5–20× faster than BeautifulSoup for large crawls. |
| **Playwright** | Apache-2.0 | Adopt (already optional) | Pages that need JavaScript. Ship the Chromium build inside the image, never as a download on first run. |
| **Crawl4AI** | Apache-2.0 | Assess | Good Markdown output for RAG, but it duplicates our crawler and pulls Playwright with it. Borrow its ideas. |
| **Scrapy** | BSD-3 | Assess | Only if a site needs crawls of millions of pages. |
| Firecrawl | AGPL-3.0, cloud-first | Avoid | Licence and design both point to a hosted service. |
| **SearxNG** (for agent web search) | AGPL-3.0, separate service | Adopt (already, profile `search`) | Self-hosted metasearch. On an air-gapped site, point it at intranet search engines only. |

### 2.2 Parsing, OCR and chunking

**Today:**
- `pypdf`, `python-docx`, `openpyxl`, BeautifulSoup and the standard-library email parser.
- A clause-aware chunker that keeps clause ids, tables and section paths.
- **Gap:** scanned PDFs and Hindi OCR (roadmap Phase 1).

| Library | Licence | Verdict | Why |
|---|---|---|---|
| **Docling** (IBM) | MIT | **Adopt** as `[parse]` extra / sidecar | Layout, reading order and tables come out as structured data, with OCR plug-ins. Its layout and table models are fetched once with `docling-tools models download` into the bundle. It gives better table fidelity than `pypdf`, which matters for schedules and BOQs. |
| **LiteParse** (LlamaIndex) | Apache-2.0 | Trial | A fast, local, layout-aware parser with built-in OCR, needing no GPU and no cloud API. It is a lighter first pass than Docling on CPU-only boxes. Benchmark both on our scanned set before choosing a default per profile. |
| **Tesseract 5** + `hin`, `eng`, `san` traineddata | Apache-2.0 | **Adopt** | The baseline OCR for Devanagari. It runs on CPU, and its language files ship in the image. |
| **PaddleOCR** (PP-OCRv5) | Apache-2.0 | Trial | Higher accuracy on Hindi and mixed scripts. Weights go in the bundle; it runs as a GPU sidecar. |
| **Unstructured** (open-source core) | Apache-2.0 | Assess | Very broad format coverage but heavy dependencies. Docling covers our formats. |
| **MinerU** | Custom licence based on Apache-2.0 (earlier versions AGPL) | Assess | Strong on PDFs. Check the current LICENSE file before any bundling. |
| Marker / Surya | GPL code, restricted weights | Avoid | Licence. |
| **Chonkie** | MIT | Assess | Keep our clause-aware chunker. Chonkie's semantic chunker could serve as a fallback for prose without structure. |
| LlamaIndex / LangChain as a framework | MIT | Avoid as a core dependency | Large dependency trees, fast API churn and optional telemetry, and they would hide our access checks. Use single components (such as LiteParse) where they pass the rules. |

### 2.3 Embeddings

**Today:**
- `Qwen/Qwen3-Embedding-0.6B`, multilingual including Hindi, served by Hugging Face TEI (GPU or CPU image).
- **Gap:** the GPU TEI image tag is not pinned by digest yet.

| Model / server | Licence | Verdict | Why |
|---|---|---|---|
| **Qwen3-Embedding 0.6B / 4B** | Apache-2.0 | **Adopt** (current) | Strong multilingual quality; 0.6B fits beside the LLM on 12 GB. |
| **BGE-M3** | MIT | Trial | Dense, sparse and multi-vector from one model. Good for Hindi and English hybrid search, and enables late interaction in Qdrant. |
| **multilingual-e5-large-instruct** | MIT | Assess | A fallback if Qwen3 has trouble on an Indic script. |
| **Infinity** server | MIT | Assess | An alternative to TEI that serves several encoders in one process. |
| Jina v3 / v4, OpenAI, Cohere, Voyage | Non-commercial weights or cloud | Avoid | Rules 1 and 3. |

### 2.4 Vector store

**Today:** Qdrant for the cluster and GPU profiles; a NumPy index for development and tiny installs. PostgreSQL holds metadata, access records and the audit log.

| Store | Licence | Verdict | Why |
|---|---|---|---|
| **Qdrant** | Apache-2.0 | **Adopt** (current) | Payload filters carry our access records. It has multi-vector support and snapshots, and scales out by sharding. |
| **pgvector** (with `pg_trgm` / full-text search) | PostgreSQL | Trial | A one-database profile for small offices: PostgreSQL only, one less service to run and back up. |
| Weaviate, Milvus | BSD / Apache-2.0 | Assess | No gap they fill that Qdrant doesn't. |
| Pinecone | Cloud | Avoid | Rule 1. |

### 2.5 Retrieval and reranking

**Today:**
- Hybrid search: BM25 plus dense vectors, both inside the asker's access.
- A second access check, version and "as of" rules, and amendment chains.
- Rerank with `BAAI/bge-reranker-v2-m3` on TEI.

| Technique / library | Licence | Verdict | Why |
|---|---|---|---|
| **bge-reranker-v2-m3** | Apache-2.0 | **Adopt** (current) | Multilingual cross-encoder. |
| **Qwen3-Reranker 0.6B** | Apache-2.0 | Trial | Same family as the embedder; compare it on the golden set. |
| **HyDE** (hypothetical answer as the query) | Technique | Trial, per agent | It helps vague questions, but costs one LLM call. Turn it on in research agents, off in the classic ones. |
| **Late interaction** (BGE-M3 multi-vector, ColBERT) | MIT | Assess | Better recall on long clauses; needs more storage. |
| **tantivy** (BM25 in Rust) | MIT | Assess | Only if BM25 becomes the bottleneck beyond about 1 million chunks. |
| Jina Reranker | Non-commercial weights | Avoid | Rule 3. |

### 2.6 Generation (LLM serving)

**Today:**
- vLLM serving Qwen3 AWQ, picked by GPU profile (6 to 24 GB), with Hermes tool calling.
- With no GPU, answers are verified extracts.
- The roadmap §6 covers fitting large models on small GPUs.

| Server / model | Licence | Verdict | Why |
|---|---|---|---|
| **vLLM** | Apache-2.0 | **Adopt** (current) | Throughput, prefix caching, AWQ and FP8, and tool calling. |
| **llama.cpp server** (GGUF) | MIT | **Adopt** for `gpu-6gb` and CPU-with-LLM | It runs partly or fully on CPU, so a small LLM works without a GPU. |
| **Ollama** | MIT | Trial (developer convenience) | Easiest local start. Models must be imported from the bundle, never pulled. |
| **SGLang** | Apache-2.0 | Assess | Faster structured output; revisit when we use constrained decoding heavily. |
| **Qwen3 family** (4B / 8B / 14B / 30B-A3B) | Apache-2.0 | **Adopt** (current) | Bilingual, with tool calling and ungated weights. |
| **IndicTrans2** (AI4Bharat) | MIT | Trial | Offline translation between English and 22 Indian languages, for ingestion and answers. |
| **Sarvam** / other Indic LLMs | Check each model card | Assess | Strong Indic models; licences vary by release. |
| Llama, Gemma | Custom terms | Avoid as default | Usage terms; an operator may choose them. |
| Claude, GPT, Gemini | Cloud | Avoid for air-gap | Rule 1. They are allowed only as an operator-configured route on connected sites, behind the egress guard and PII masking. |

### 2.7 Agent harness and tools

**Today:**
- Smriti's own runtime: classic, agentic, research and workflow agents.
- An approval gate that is forced after the agent reads untrusted content.
- A sandbox container for Python, MCP client and server, and OpenAI-compatible endpoints.

| Library | Licence | Verdict | Why |
|---|---|---|---|
| **MCP Python SDK** | MIT | **Adopt** (current) | A standard tool protocol in both directions. |
| **Agent Skills format** (`SKILL.md` folders) | Open format | **Adopt** | Smriti skills already use this layout: front matter, instructions and code. Next step: import third-party skill folders as tenant skills, with validators reviewed by a curator. |
| **gVisor** (`runsc`) | Apache-2.0 | Trial | Stronger isolation for the code sandbox on Linux hosts. |
| LangGraph, CrewAI, AutoGen, smolagents | MIT / Apache-2.0 | Avoid as runtime | They would bypass our act-as-asker and approval rules. Read them for patterns only. |

### 2.8 Evals, guardrails and observability

**Today:**
- Golden and red-team sets with release gates in CI.
- The sentence verifier (support, numbers, negation).
- Injection scanning, PII masking and a hash-chained audit log.

| Library | Licence | Verdict | Why |
|---|---|---|---|
| **Ragas** | Apache-2.0 | **Adopt** in CI and `smriti eval --ragas` | Faithfulness, context precision and recall, scored by a **local** judge (our vLLM route). Set `RAGAS_DO_NOT_TRACK=true` in the image. It adds a second opinion beside our own gates; it doesn't replace them. |
| **lm-evaluation-harness** (EleutherAI) | MIT | **Adopt** for model qualification | Run before a quantised model joins a profile: an MMLU-style suite plus Hindi tasks, with the datasets fetched into the bundle. |
| **DeepEval** | Apache-2.0 | Assess | Overlaps with Ragas. Telemetry is on by default (`DEEPEVAL_TELEMETRY_OPT_OUT=YES`). |
| **promptfoo** | MIT | Trial | Red-team generation for prompt injection and jailbreaks. It needs Node; run it in CI only. |
| **Granite Guardian / Qwen3Guard** | Apache-2.0 | Trial | A local guard model for the existing `guard` route: harm and jailbreak scores beside our rules. |
| **NeMo Guardrails** | Apache-2.0 | Assess | Dialogue rails. Our rules already cover the pilot bar. |
| Guardrails AI | Apache-2.0, but its hub fetches validators online | Avoid as a dependency | Rule 1. Copy single validators if needed. |
| **OpenTelemetry + Prometheus + Grafana** | Apache-2.0 / AGPL (Grafana as a service) | **Adopt** | Metrics and traces stay on site. Smriti already serves Prometheus metrics at `/api/metrics`; OTLP traces and Grafana dashboards are next. |
| **Langfuse** (self-hosted) | MIT core | Trial | LLM traces and prompt versions. It needs ClickHouse, Redis and S3-compatible storage, so it goes in the `observability` profile only for sites that want it. |
| **Arize Phoenix** | Elastic License 2.0 (check before shipping) | Assess | Fine to run internally; not to offer as a hosted service. |

### 2.9 Deployment and scaling

**Today:** a Docker Compose install with hardware profiles, images pinned by digest, and `requirements.lock` with hashes.

| Tool | Licence | Verdict | Why |
|---|---|---|---|
| **Offline bundle** (`deploy/offline/`) | Ours | **Adopt** (added now) | One folder with images, model cache, source, settings and checksums. See §3. |
| **Helm chart** (k3s / RKE2 / OpenShift) | Ours | Next | Scale out: API and worker replicas, Qdrant shards, a vLLM pool per GPU. |
| **k3s** | Apache-2.0 | Adopt for multi-node air-gap | Single binary, with an official air-gap install (image tarball). |
| **Zarf** | Apache-2.0 | Trial | Packages Helm charts, images and files into one signed air-gap archive. It is the cluster version of our bundle. |
| **Harbor** | Apache-2.0 | Trial | A site-local registry for multi-node sites. |
| **cosign** (key-based, offline verify) and **syft** SBOM | Apache-2.0 | Adopt | Sign the bundle and verify on site without Sigstore's online services. |
| **Trivy** with an offline vulnerability DB | Apache-2.0 | Adopt | Scan images inside the air gap; the database is refreshed through the bundle. |
| Cloudflare, AWS Bedrock, Modal | Cloud | Avoid | Rule 1. |

## 3. One-click air-gapped install

There are two scripts, in [deploy/offline/](../deploy/offline/).

**On a machine with internet:**

```bash
./install.sh                         # once, with the site's profile; wait for "ready" (fills the model cache)
deploy/offline/make-bundle.sh        # writes smriti-offline-<version>/
```

The bundle contains:

| File | Contents |
|---|---|
| `images.tar` | Every image `docker compose config --images` lists, built and pulled |
| `hf-cache.tar` | Every model the profile uses |
| `smriti-src.tar` | `git archive` of this commit |
| `site.env` | The `.env` with `HF_HUB_OFFLINE=1` and no `HF_TOKEN` |
| `SHA256SUMS` | Checksums for all of the above |

**On the air-gapped machine:**

```bash
./install-offline.sh                 # verify checksums → docker load → restore models → compose up --pull never
```

`install-offline.sh` makes no network connections: images are loaded, never pulled, and the model cache is restored into the compose volume. Nothing is built.

**Still to do** for the Phase 1 card:

- Sign `SHA256SUMS` with cosign.
- Add an offline wheelhouse for the `pip` path.
- Pin the GPU TEI image by digest.
- Ship Tesseract and Docling models in the image.
- Do a timed dry run on a clean machine with the network cable out.

## 4. Skills and agents in the box

### Built-in skills

They live in `src/smriti/builtin/skills/` and every tenant gets them. A pack can replace one by shipping a skill with the same name. As with every Smriti skill, the code decides and the model only drafts.

| Skill | Grants | Purpose |
|---|---|---|
| `pii-scan` | view | Counts personal data in a document by category and flags a public document holding PII, or PII that isn't masked by role. It never shows the values (DPDP Act hygiene). |
| `compare-documents` | view | A paragraph-level redline between two documents or editions, with counts of added, removed and changed paragraphs and a similarity score. |
| `deadline-tracker` | view | Every date and "within N days" obligation, with clause and sentence, due dates from a start date, and a CSV. |
| `rti-reply` | view, draft | A point-wise RTI reply from the library: 30-day or 48-hour deadline under section 7(1), possible section 8(1) exemptions flagged, "not held" and transfer under section 6(3) marked. It never quotes a passage flagged for injection, and the draft is maker-checker. |
| `citation-check` | view | Verifies pasted text sentence by sentence: supported, wrong number, reversed rule, superseded only, or unsupported. |

The railways pack keeps `validate-tender`, `validate-acceptance-letter` and `circular-impact`.

### Agent inventory

It lives in `src/smriti/agents/inventory.py`, is served in `/api/agents/catalog` as `agent_templates`, and is shown in **Agents → Start from the inventory**. All 17 templates use only built-in tools, so all of them work offline.

| Purpose | Agents |
|---|---|
| Answer questions | Tender desk · Policy helpdesk · हिंदी सहायक (Hindi helpdesk) · Onboarding buddy |
| Check and comply | Contract analyst · Fact checker · Privacy officer · Circular impact watch |
| Draft letters and replies | RTI desk · Noting and letter drafter · Minutes and action items |
| Analyse documents and data | Data analyst · What changed? · Deadline tracker |
| Research | Research brief · Web and intranet researcher |
| Run the office | Case chronology |

**Next candidates** (they need new connectors or a model, so they aren't in the box yet):

- Grievance (CPGRAMS) reply drafter.
- Procurement (GeM) bid comparer.
- Audit-para reply assistant.
- Parliament-question brief.
- An HR rules desk.

### Sources

- [LiteParse announcement](https://www.llamaindex.ai/blog/liteparse-local-document-parsing-for-ai-agents) and [repository](https://github.com/run-llama/liteparse) (Apache-2.0).
- [MinerU repository](https://github.com/opendatalab/MinerU) (check the current LICENSE).
