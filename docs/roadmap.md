# Smriti roadmap: audit, competition and plan

Date: 3 November 2026. Produced by a seven-member review council (Architect, Skeptic, Tuner, Oracle, Scout, Advocate, Strategist) over three rounds of position, challenge and convergence. The full deliberation is in `.claude/council/sessions/smriti-audit-and-enhancement-plan-20261103-1210/`.

**Read this first.**
- Nothing in this plan has been measured on a real GPU. Every tokens/s and latency figure below is a projection with low to medium confidence until Phase 0 runs on an RTX 3060 and an RTX 5070 Ti.
- Security findings were traced through the code, not exploited.
- Competitor facts are mostly from 2026 secondary sources. Each row carries a confidence mark.

## 1. Summary

Smriti's core is ahead of the field on trust. It has:
- two-point permission enforcement with a leakage suite;
- "as of" answers over amendment chains;
- sentence-level citation checks with abstention;
- role-based PII masking;
- a hash-chained audit log;
- answers with no model at all.

No competitor we found has more than one of these.

The weak parts sit around that core:
- identity is a trusted header;
- several flows can leak or escalate;
- the verifier has blind spots, such as ignoring "not";
- nothing has run on a real GPU;
- the UI is a developer demo.

The council agreed on one order of work:

1. **Gate 0 (weeks 1-3): safe and measured.** A two-week security and data-integrity release (both audits), shipped first. In parallel, measure everything on the two real cards and fix the verifier's free bugs.
2. **Phase 1 (weeks 3-13): sovereign pilot kit.** The platform spine (migrations, per-version access, OIDC, central authorisation, serialised audit), a hardening track, Hindi OCR, a better verifier, a thin UX layer, an offline bundle and an external security test.
3. **Phase 2 (weeks 8-11, overlapping): public launch.** Launch on the moat: a public Trust benchmark against competitors, measured small-GPU numbers, Ollama presets and a unified chat home.
4. **Phase 3 (weeks 14-21): pilot to reference.**
5. **Phase 4 (weeks 20-30): enterprise table stakes.** SharePoint, Drive and Confluence with permission sync, SCIM, settings UI.
6. **Phase 5 (week 24+): ecosystem.**

Breadth is frozen until the pilot gives signal. That covers more channels, more workflow nodes, more connectors, Kubernetes and multi-tenancy.

Assumes one founder plus one hire by week 4. Without the hire the launch still holds at week 11 and the pilot moves to about week 16-17.

## 2. Strengths to keep

| Strength | Where | Status vs competitors |
|---|---|---|
| Two access-enforcement points (filter in search, bulk re-check), leakage suite in CI | `engine.py`, `access.py`, `evals.py` | No competitor publishes leakage evals |
| Supersession and amendment graph, "as of" answers, circular impact | `engine.py`, `watch.py`, `skills.py` | Unique among the products compared |
| Sentence- and number-level citation verification with abstention | `guardrails.py`, `ask.py` | Others show citations; none verify them |
| Field-level PII masking by role, masked index (Saaf) | `privacy.py` | Unique among open-source tools |
| Hash-chained audit, skills with coded validators | `audit.py`, `skills.py` | Rare |
| Works with no model; extractive-first answering saves GPU time | `llm.py`, `ask.py` | Unique |
| One-click Docker profiles for 6-24 GB GPUs and CPU | `deploy/` | Lighter than Onyx, RAGFlow, Dify |
| Ingestion preview, per-item job results, egress guard, sealed credentials | `connectors/`, `egress.py`, `credentials.py` | Good |
| Agents with tools, approvals, tainted-run rule, workflows, MCP both ways | `agents/`, `mcp_server.py` | At parity with or ahead of DocsGPT |

## 3. Audit findings

Severity: **C** critical, **H** high, **M** medium, **L** low. Paths are relative to `src/smriti/`.

### 3.1 Security

All of these were traced through the code but not exploited. They are fixed in the **Gate 0 release** (G0-x) or the **hardening track** (H-x), section 7.

| Id | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| S-01 | C | An uploader can make a new upload land on an existing document's id, through the filename stem, `ref_no` or front-matter `doc_id`/`access`. The new version resets the access label, and access is stored per document, so every earlier confidential version becomes readable. The uploader can also replace the official text. | `engine.py:227-262,840-848`, `api.py:491-510` | G0-1 stop-gap, then per-version access (M1) |
| S-02 | C | Demo auth is the shipped default. Identity comes from `X-Smriti-User`, then `?as=`; a request with neither acts as the first directory user. Only the 127.0.0.1 bind protects it. | `api.py:141-166`, `config.py:42,69`, `_common.env:10` | G0-2, then OIDC (M2) |
| S-03 | H | A fixed-identity website widget acts as its creator (usually a curator) behind a fakeable Origin check. It also skips the `max_class` and PII filters that Slack, Telegram and Discord replies get. | `channels.py:110-135,248-259,327-360`, `channels_api.py:158-195` | G0-3 |
| S-04 | H | Curator-configured SQL, REST and MCP tools, with their stored credentials, can be used by any viewer through `/api/agents/preview`. The tool `enabled` flag is never checked. | `agents/api.py:206-216`, `agents/__init__.py:285-306` | G0-4 |
| S-05 | H | Tainted runs only gate tools that change data. A prompt injection can still send data out through `read_url` or web search, and documents flagged for injection never taint a run. | `agents/runtime.py:399-445`, `agents/tools.py:274-305` | G0-5, H-2 (URL provenance) |
| S-06/07 | H | The sandbox is one long-lived container with a shared `/tmp` and one UID, and child processes outlive the timeout. Local sandbox mode on Windows lets any viewer run code on the host. | `agents/sandbox.py:79-128`, `compose.yaml:80-96` | G0-6, H-1 (per-run isolation) |
| S-08 | H | Supply chain: Saaf is installed from git `@main`, `searxng:latest` is unpinned, and there is no committed lockfile or hashes. | `pyproject.toml:15`, `compose.yaml:100` | G0-7 |
| S-09 | H | The audit chain is serialised by an in-process lock. Two API workers plus the job worker can fork it, which makes `verify` report tampering that never happened. The hash has no key. | `audit.py:33-51` | M5 |
| S-10 | M | The answer cache key has no tenant id, and tokens are not bound to a tenant. Answers could leak between tenants once more than one pack is loaded. | `ask.py:102,192`, `models.py:138-144` | G0-8 |
| S-11 | M | Shared links never expire and show tool-result content to the viewer. | `agents/__init__.py:449-477` | H-4 |
| S-12 | M | DNS rebinding and IPv4-mapped addresses can get past the egress guard. Agent tools allow private networks. | `egress.py` | H-3 |
| S-13 | M | Uploads, zip archives and workflow expression nesting have no size or depth limit. | `api.py:503`, `engine.py:408-425`, `agents/expr.py` | G0-9 |
| S-14 | M | API keys never expire, have no scopes, and can approve their own runs. | `agents/__init__.py:482-544` | G0-5, H-5 |
| S-15 | M | XSS sink through an inline `onclick`; no CSP. | `web/app.js:468` | G0-10 |
| S-16 | M | The Fernet key sits on the same volume as the database, and Postgres has a default password. | `credentials.py`, `compose.yaml:27` | H-7 |
| S-17 | M | The SQL tool's read-only checks and timeouts are uneven across dialects. | `agents/tools.py`, `connectors/sql.py` | H-6 |
| S-18 | L | `/api/sources`, `/api/policy`, presets, deep health and metrics have no grant check. | `api.py:174-226,515-518,849-857` | G0-11 |
| A-6 | H | Approval resume has no atomic status check, so a double approval runs a mutating tool twice. | `agents/runtime.py:163-232` | G0-12 |
| A-7 | H | The PII restore map is stored in plaintext in `agent_runs.state`, which undoes masking at rest. | `agents/runtime.py:145` | G0-13 |

### 3.2 Architecture and code quality

| Sev | Finding | Evidence | Fix (spine item) |
|---|---|---|---|
| C | Access is stored per document, not per version (the root cause of S-01). | `store.py:43-80` | M1 |
| H | No schema migrations: `md.create_all` across 20+ tables. The first column change breaks existing installs. | `store.py:470` | P4 Alembic baseline |
| H | No identity layer: no OIDC or LDAP, and the directory is a static YAML file with no deprovisioning. | `api.py:141-166`, `roles.yaml` | M2, M3 |
| H | Cross-process state lives in Python memory: audit lock, scheduler de-duplication, ingest lock, answer and vector caches, admission semaphores. Each worker repeats crawls and scheduled agents. | `audit.py`, `watch.py:695`, `engine.py:227`, `llm.py:107` | M5, M6, P2 |
| M | Two ingestion paths, pack sources and UI connectors, with different behaviour. | `watch.py`, `connectors/` | M6 |
| M | `api.py` is about 1,075 lines with mixed error shapes and no API version. | `api.py` | M9 (`/api/v1`, routers) |
| M | Every ingest bumps the index generation, which clears the answer and access caches. Access re-checks scan all documents. | `engine.py:161-188` | Tuner F8 |
| M | Streaming endpoints hold the sync threadpool for the whole LLM call. | `api.py:277` | P3 async streaming |

### 3.3 Answer quality (RAG, verifier, agents, evals)

| Sev | Finding | Evidence | Fix |
|---|---|---|---|
| C | The verifier ignores negation: "not", "no", "any" and "all" are stopwords, so "shall not exceed" counts as supported by "shall exceed". | `index.py:40-45`, `guardrails.py:197-199` | Oracle item 1, then NLI |
| C | An answer passes when 60% of its sentences are supported, so 2 made-up sentences out of 5 ship. | `guardrails.py:207,218`, `config.py:253` | Per-sentence drop or flag (item 2) |
| C | Hindi answers over English sources score about zero support. Devanagari digits and dates are not normalised. | `guardrails.py:73,197-229` | Inline quotes + multilingual NLI (items 6-7) |
| H | The release gate checks retrieval and leakage only. Faithfulness and abstention are computed but not gated. The set is 14 synthetic questions. | `evals.py:125-130` | Eval harness v1 (item 5) |
| H | No real model, agent or tool call has ever been evaluated. Only scripted fakes. | `evals.py:41`, tests | Nightly real-model runs per profile |
| H | Hindi retrieval: English-only stemming and route patterns; cross-lingual recall relies on glossary synonyms. | `index.py:86-110`, `ask.py:33-47`, `engine.py:562-568` | BGE-M3 + Hindi stripper (item 10) |
| H | Agent context grows with no budget. Tool output is cut at 8,000 characters, mid-passage, and prompts reach about 15k tokens against 8k contexts, failing at step 3-4. | `agents/runtime.py:322-353,446-453` | F2 context budget (item 8) |
| M | "As of" filtering happens after top-50 fusion, so historic versions can be missed. | `engine.py:598-655` | As-of second pass (item 11) |
| M | The semantic cache can serve the answer to a question with flipped negation. | `ask.py:78-89` | Polarity in cache key (item 4) |
| M | Tokens are streamed before verification. | `ask.py:279-284` | Stream only verified sentences |
| M | The local index and Qdrant score sparse matches differently, so local eval results don't carry over. | `index.py:368-379,427-431` | Parity test (item 13) |
| M | No OCR, layout or PDF tables. Scanned circulars are flagged and skipped. | `parsers.py:224-268` | Docling + Tesseract/PaddleOCR (Phase 1) |
| M | The research agent plans without seeing any retrieval results and runs sub-questions one after another. | `agents/runtime.py:523-631` | Item 9 |

### 3.4 Performance

| Sev | Finding | Evidence | Fix |
|---|---|---|---|
| H | Agentic answers are not streamed; tool steps use non-streaming calls. | `agents/runtime.py:326,355` | F1 streaming tool loop |
| H | Admission control is per process and blind to the engine: 8 per worker × 2 workers can hit an engine with 2 slots. | `llm.py:107-118` | F4 shared, slot-aware admission |
| H | `smriti bench` has no time to first token, prefill and decode rates, or agent scenario. | `bench.py:48-110` | Bench v2 (model servers: `smriti modelbench`, done; agent scenario open) |
| M | Two full-table relation scans per retrieve; N+1 amendment lookups. | `engine.py:629-630,710-757` | F5/F6 memoise and batch |
| M | The reranker processes about 25k tokens per query (50 × 2,000 chars) in serial batches, on the GPU shared with the LLM. | `config.py:118`, `index.py:566-573` | F9 token cap (after the as-of second pass) |
| M | Three TEI encoder containers. | `compose.yaml` | One encoder process (BGE-M3, reranker, NLI) |
| L | The extractive answer is streamed one word at a time with a sleep. | `api.py:285-292` | Chunked stream |

### 3.5 User experience

Read from code; not checked in a browser.

| Sev | Finding | Evidence | Fix (UX item) |
|---|---|---|---|
| P0 | Nav items are `<a>` without `href`; there is no focus ring, and citations, rows and conversation items are clickable spans. Keyboard and screen-reader users are locked out. | `index.html:18-25`, `app.css:47-68`, `app.js:122,205,277`, `agents.js:342` | U1 accessibility baseline |
| P0 | The UI is English only, `lang="en"` is set even on Hindi answers, and no Devanagari font is bundled. | `index.html:2`, `app.css:23` | U2 Hindi on officer screens |
| P0 | There is no sign-in; anyone can use "Viewing as", which shows clearance and unmask chips. | `app.js:40-49,601-610` | U3 sign-in, demo mode labelled |
| P1 | Ask has no history, no new chat and no agent picker, while agent chat does. Two chat UIs behave differently. | `app.js:4,88-119` vs `agents.js:338-385` | U4 history; U13 one chat home |
| P1 | Approvals have no inbox, no list API and no notification, and show raw JSON. Leaving the page strands the run. | `agents.js:295-297`, `agents/api.py:218` | U8 readable approvals + pending list |
| P1 | No first-run flow or settings UI; setup means editing `.env` and YAML. Errors show as raw "500: …". There is no model-offline state. | `install.sh:113`, `app.js:19-23,175` | U5 errors + offline banner; U11 first-run; U12 model card |
| P1 | Access on upload is free text with commas and `*`, so a typo can expose or hide a document. | `ingest.js` | U7 access pickers |
| P1 | The drawer is not a dialog and toasts are not announced. The workflow canvas is mouse-only. | `index.html:41-42`, `app.js:27-31`, `workflows.js:181-221` | U1; outline editor in Phase 4 |
| P2 | The Govern audit list is capped at 60 rows with no filter or export; review work is split across Memory, Skills and Govern. | `app.js` | Admin console (Phase 1), review queue (Phase 3) |

### 3.6 Second audit (AUDIT_FINDINGS.md, 3 November 2026)

An independent first-pass audit reproduced several bugs with scratch scripts. I spot-checked it against HEAD `60cc21b`:
- **Confirmed in code:** #8 (`models.py:18`: a clearance of `"Secret"` ranks above `secret`, and SSO claims pass the raw string at `access.py:54`), #9 (`watch.py:126` uses a plain `httpx.Client`), #11 (`engine.py:870`: an unreachable clamd returns `scanned: False` and ingest continues) and #13 (`store.py:583` matches any version of any document).
- **Not in this repo:** #15 (`llm_rag_evaluation/webui/`) belongs to another project.
- **Testing gap does not apply here:** "pytest not installed" was the auditor's environment. The dev venv runs all 80 tests.

| # | Sev | Finding | Status in this roadmap | Where it is fixed |
|---|---|---|---|---|
| 1 | C | Filename-stem `doc_id` overwrites another user's doc and resets access | Same as S-01 | G0-1 |
| 2 | C | Front matter overrides `access`, `doc_id`, `supersedes`; a crawled page can retire a real circular | Same as Architect's critical; **`supersedes` is new** | G0-1 + G0-14 |
| 3 | H | Site outage finishes "done" and `on_removed=tombstone` hides every doc | **New** | G0-15 |
| 4 | H | Ingest writes the DB before embedding; a failed embed leaves the doc unsearchable, and a retry says "duplicate" | **New** | G0-16 |
| 5 | H | Audit chain forks with several workers | Same as S-09 | G0-17 (advisory-lock stop-gap now), M5 |
| 6 | H | Widget ignores `max_class`/`mask_pii`; Origin check bypassable | Same as S-03 | G0-3 |
| 7 | H | Demo auth on non-loopback bind; tokens not tenant-bound; metrics open | Same as S-02, S-10, S-18 | G0-2, G0-8, G0-11 |
| 8 | H | Unknown clearance fails open | **New** | G0-18 |
| 9 | H | Pack-source crawler bypasses the egress guard (SSRF) | **New** | G0-19 |
| 10 | M-H | `title`/`section`/`ref_no` unescaped inside `<passage>`, so a `</passage>` breakout works; chat `history` skips the guard and PII masker | **New** | G0-20 |
| 11 | M | Malware scan fails open | **New** | G0-21 |
| 12 | M | Failed downloads go into the snapshot and are never retried | **New** | G0-22 |
| 13 | M | `version_by_sha` matches old versions and tombstoned docs, so reverts and re-uploads say "duplicate" | **New** | G0-16 |
| 14 | M | Faithfulness ignores negation; cache reuses answers across negation | Same as Oracle items 1 and 4 | Phase 0 Track C |
| 15 | M | Evaluation web UI bugs | Not this repo | n/a |

The 20 smaller bugs and the gaps go into a **Phase 0 Track D (bug sweep)**, each with a regression test:
- **Ingestion:**
  - access oracle on `/documents/{id}/access`;
  - 500s on an unknown user, a corrupt PDF, a Hindi download filename, and a NUL byte in a question;
  - uploads read fully before the size check;
  - Hindi `.txt` rejected by the 4 KB sniff.
- **Parsing:**
  - `_split` duplicates text with no sentence delimiters;
  - `CLAUSE_HEAD` treats dates as clauses;
  - a markdown file starting with `---` loses its first section;
  - hidden-text detection misses `display:none` divs;
  - semicolon CSVs collapse into one column;
  - empty or JS-only pages ingest with 0 chunks and no warning.
- **Scheduler:**
  - a bad cron string kills the Scheduler thread;
  - named weekdays (`mon`) never fire;
  - `Thread._stop` is shadowed by an Event, breaking `join()` on Python 3.12.
- **Retrieval and answers:**
  - curators can approve memories derived from docs they can't read, and "erase" only blanks the text;
  - an empty `choices` list from the model gives a 500;
  - SQL row ids use per-process `hash()`;
  - `_amendments` matches clause ids as substrings;
  - "how many days…" hits the table-sum path with faithfulness forced to 1.0;
  - `/api/stats` verifies the whole audit chain every 6 s.
- **Gaps:**
  - no REST route to delete or tombstone a document;
  - deleting a connector leaves its docs behind;
  - crawler: no JS rendering warning, no auth, sitemap indexes parsed as HTML, `robots.txt` 5xx/429 treated as allow-all, no streaming byte cap or charset handling.
- **Missing test suites:** fault-injection, multi-process, prompt-injection and crawler-fixture suites, plus Playwright UI tests.

## 4. Key flows

| Flow | Today | After Phase 2 |
|---|---|---|
| Install to first answer | Docker: up to 30 min with no progress; the browser doesn't open on Linux; pip needs GitHub for Saaf. | Signed images or offline bundle; the installer opens the browser on a first-run screen: model status with a Test button, then load the demo or add a source. Target: under 10 minutes, measured by a non-founder. |
| Sign-in | "Viewing as" switcher, or static tokens. | OIDC through Keycloak, AD FS or Entra, or an mTLS trusted proxy. Roles come from IdP groups. Demo mode works on loopback only, with a banner. |
| Connect a source | Strong: preview, jobs, per-item results. Access is free text. | Access pickers; connector access labels only ever tighten; folder ACLs synced from NTFS/AD. |
| Ask and check citations | Streaming, "as of", evidence panel. History is lost on reload. | One chat home with history and an agent picker. Each sentence is shown only once its quote and NLI check pass. Sources first. Hindi UI. PDF highlight comes in Phase 3. |
| Agent run with approval | Inline only; raw JSON arguments. | Pending-approvals list with a nav badge and readable arguments. Any data leaving the system after outside content was read needs approval. A run can be approved once only. |
| Embed widget or bot | Good setup steps; a fixed widget acts as its curator. | The widget acts as a public-only service principal; every channel applies classification and PII filters. |
| Curator review | Split across Govern, Memory and Skills. | Admin console (users and roles, job status, filterable and exportable audit, model health) and a golden-set authoring form. A single review queue comes in Phase 3. |

## 5. Competitive position

### 5.1 Matrix

Confidence: **H** = repo, or a 2026 source agreeing with prior knowledge. **M** = one 2026 secondary source. **L** = prior knowledge only. "After P2" is planned, not built.

| Capability | Smriti today | Smriti after P2 | DocsGPT | Onyx | RAGFlow | Dify | Open WebUI | AnythingLLM | Glean | Conf. |
|---|---|---|---|---|---|---|---|---|---|---|
| Licence | Apache-2.0 | Apache-2.0, trust features in core | MIT | MIT core + EE | Apache-2.0 | Custom; SSO paid | Custom (branding clause) | MIT | Proprietary | H/L |
| Air-gapped self-host | Yes, plus no-model mode | + offline USB bundle | Yes | Yes | Yes | Yes | Yes | Yes | No | H/M |
| Small GPU / local engines | vLLM profiles 6-24 GB | + Ollama, LM Studio, llama.cpp presets; measured numbers | Ollama | Ollama, heavy stack | Heavy stack | Ollama plugin, heavy | Ollama-native, best local UX | Easiest local | n/a | M |
| Connectors | 8 generic | + folder ACL interface | Files, web (SharePoint on roadmap) | 40+ | Few | Files, Notion, web, plugins | Files, web | Files, GitHub, Confluence | 275+ | H/M |
| Source permission sync | Own ACL model | Folder/AD sync in leakage suite | No | EE only | No | No | No | No | Yes | M |
| SSO / LDAP | No | OIDC + LDAP via Keycloak | On roadmap | OIDC (SAML EE) | Partial | Paid | OIDC/LDAP | Basic | Yes + SCIM | M |
| Admin / RBAC | Roles, no console | Minimal console | On roadmap | Yes | Teams | Yes | Yes | Basic | Yes | M/L |
| Audit | Hash chain | HMAC, DB-serialised, exportable | Roadmap | EE | Basic | EE | Basic | Basic | Yes | H/L |
| PII masking by role | Yes (Saaf) | Same | No | No | No | Plugin | No | No | DLP | H/L |
| OCR / layout / tables | No | Hindi + English OCR, Docling | Basic | Option | **Best** (DeepDoc) | Plugins | Option | Basic | Yes | M |
| Versions / "as of" / amendments | **Yes** | Yes | No | No | No | No | No | No | No | H/M |
| Verified citations + abstention | **Yes** | + NLI per sentence | Citations | Citations | Citations | Citations | Citations | Citations | Citations | H/M |
| Published trust / leakage evals | CI gate (synthetic) | **Public Trust benchmark** | None found | None | None | None | None | None | None | M |
| Agents / workflows / MCP | 3 agent kinds, approvals, workflows, MCP | Frozen; "Extend with MCP" guide | Workflow builder on roadmap | Agents, MCP | Agent canvas | **Strongest** canvas, 120+ plugins | Pipelines | No-code agents, MCP | Agents, MCP | M |
| Indic languages | Hindi (glossary) | + Hindi UI, Devanagari-aware verifier | Model-dependent | Model | Model | Model | UI i18n | Model | Model | H/L |
| Community (stars) | New | Target 1k in 30 days | ~17k | ~16-20k | ~78-92k | ~155k | ~100k+ | ~53k | n/a | M/L |

Also relevant, though not open governance tools:
- **Sarvam** (air-gapped platform, many Indian languages) and **CoRover BharatGPT** (offline appliances, MeitY empanelment) compete on procurement and models. Position Smriti as the auditable layer a systems integrator can embed alongside them.
- **Copilot** and **Gemini Enterprise** offer data residency, not on-premises deployment.
- **DocsGPT** is the closest catch-up threat: SharePoint, Confluence, OIDC/SCIM and RBAC are all on its 2026 roadmap.

### 5.2 Gaps, by when they are closed

| When | Gap |
|---|---|
| Before the pilot | OIDC + LDAP/AD (Keycloak); Hindi/English OCR with layout; minimal admin console; connector ACL interface on NTFS/AD folders |
| Before launch | Ollama/LM Studio and provider presets over the existing OpenAI-compatible gateway; minimal citation UX; "Extend with MCP" guide |
| After pilot signal | SharePoint/Drive/Confluence with permission sync, SCIM, SAML admin, settings UI (Phase 4); runtime multi-tenancy only if 2+ paying customers need it; plugin SDK (Phase 5) |

Triggers that pull a date earlier:
- DocsGPT ships OIDC/SCIM and SharePoint under MIT. SAML/SCIM and SharePoint then move to Phase 3.
- A rival publishes leakage or trust evals. The benchmark then publishes as soon as Phase 0 numbers exist.
- A tender requires SAML or a SharePoint/e-Office connector. That item then moves into Phase 1.
- RAGFlow open-sources its GPU parser. OCR is then re-benchmarked before the pilot.

### 5.3 Differentiators to double down on

1. **Answers you can sign.** Sentence and number verification with abstention, proven by a public benchmark.
2. **Time-aware memory.** "As of" answers, amendments and circular impact, generalised beyond railways through rule packs (RBI/SEBI circulars, GFR, CVC, ISO procedures, HR policies).
3. **Sovereign on one small GPU.** Apache-2.0, air-gapped, no-model mode, India-first PII masking, keyed audit, a public leakage suite, and Hindi.

### 5.4 Trust benchmark (launch centrepiece)

**Scope**
- About 300 questions over two corpora:
  - A, public Indian circulars with real amendment chains, about 20% scanned;
  - B, a synthetic enterprise set with versions, personas and fake PII.
- Eleven trap types: superseded-as-current, amendment omission, as-of date, direct and indirect permission leaks, PII exposure, number hallucination and table maths, unanswerable, citation faithfulness, injected instructions, Hindi and cross-lingual.

**Scoring**
- Canary strings in restricted documents and PII fields make leak detection a plain string match.
- Products tested: Smriti, Onyx CE, Open WebUI, AnythingLLM, DocsGPT, RAGFlow and Dify CE, on the same model and GPU, plus each product's recommended defaults. Glean is excluded because it cannot be self-hosted.
- Scores are reported per metric with confidence intervals. There is no composite score, and Smriti's losses are published.

**Reproducibility**
- Pinned Compose files, one adapter per product, and `make up / ingest / run / score`.
- A rotating 30% hold-out question set.
- Each vendor is invited to submit a tuned configuration before publication.
- Suggested neutral repo name: `rag-trust-bench`.
- Redistribution terms for the government circulars must be checked. If they're unclear, ship download scripts with checksums instead of the files.

## 6. Inference: large models on small GPUs

### 6.1 Decision

- **Day one: vLLM is the only default engine** on both cards. It is already wired, profiled and documented.
- **llama.cpp hybrid MoE** (experts in system RAM via `--n-cpu-moe`) is measured in Phase 0 and ships as an **opt-in beta** in Phase 2. The installer never picks it.

**Flip rules.** These apply per card, and no change happens before Phase 3.
- **Rule A.** llama.cpp becomes the quality default on a card only if all of these hold:
  - it passes Gate Q and Gate S on Ubuntu and on WSL2;
  - its answer correctness beats that card's vLLM quality model by at least 5 points;
  - faithfulness is at least 0.97, and the Hindi slice is no worse;
  - the beta has at most 1 open engine bug a month.
- **Rule B.** It becomes the single engine on that card (vLLM moves to 24 GB and up) only if it also passes Gate T at concurrency 8. If both engines pass, llama.cpp wins, because it is the same engine as the community's Ollama path.
- **Exactly one engine per install, always.**
- **Kill rule.** If the 12 GB hybrid's 6-step agent run has a median above 45 s, the 12 GB hybrid becomes a single-user research mode only.

Ruled out of this roadmap:
- vLLM `--cpu-offload-gb` for MoE. It re-reads offloaded weights over PCIe every step, about 3 tok/s.
- KTransformers and SGLang (a possible later workstation profile).
- NVFP4 on sm_120 (open vLLM issues).

### 6.2 Profiles

All figures are projections until bench v2 runs. VRAM figures keep about 0.6 GB for the Windows desktop.

| Id | Status | Card | Engine | Model (licence) | Key settings | RAM gate | Concurrency | Projected decode / TTFT p50 |
|---|---|---|---|---|---|---|---|---|
| t12 | Default throughput | RTX 3060 12 GB | vLLM 0.32 | Qwen3-4B-AWQ (Apache-2.0) | util 0.55, 16 seqs, 8k ctx, prefix cache | 16 GB | 16 | 70-100 single, 500-900 total / 0.2-0.5 s |
| q12-v | Default quality | RTX 3060 12 GB | vLLM | Qwen3-8B-AWQ | 4 seqs, 12k ctx; embedder and NLI on CPU | 16 GB | 4 | 40-55 / 0.4-0.8 s |
| q12-h | Beta (Phase 2) | RTX 3060 12 GB | llama.cpp | Qwen3.6-35B-A3B Q4_K_M (fallback Qwen3-30B-A3B-Instruct-2507) | `--n-cpu-moe` swept (~28-34), q8_0 KV, 32k ctx, `--parallel 2`, `--jinja --cache-reuse 256` | **32 GB, refused below** | 2 | 18-28 / 3-8 s cold, 1.5-3 s warm |
| t16 | Default throughput | RTX 5070 Ti 16 GB | vLLM (NGC image if sm_120 wheels fail) | Qwen3-8B-AWQ | util 0.70, 16 seqs, fp8 KV | 16 GB | 16 | 90-130 single, 1000-1800 total / 0.15-0.4 s |
| q16-v | Default quality | RTX 5070 Ti 16 GB | vLLM | gpt-oss-20b MXFP4 (Apache-2.0); fallback Qwen3-8B-FP8 | 4 seqs, 16k ctx, `LLM_TOOL_PARSER=openai` | 16 GB | 4 | 120-180 / 0.3-0.8 s |
| q16-h | Beta (Phase 2) | RTX 5070 Ti 16 GB | llama.cpp | Qwen3.6-35B-A3B Q4_K_M (alt GLM-4.7-Flash, MIT) | `--n-cpu-moe` ~14-20, `--parallel 3` | **24 GB** (32 recommended) | 3 | 35-55 / 1.5-4 s |

Notes for every profile:
- Thinking off.
- `SMRITI_WORKERS=1` until shared admission control lands.
- One encoder process (BGE-M3 dense and sparse, bge-reranker-v2-m3, mDeBERTa-xnli NLI), replacing three TEI containers.
- The installer checks host RAM and the WSL2 memory cap, and **refuses** a profile below its gate. It never just warns, because swapping stalls Postgres and Qdrant too.

Model licence notes:
- Gemma and Llama need legal review before government use.
- Nemotron is not a default (non-OSI licence).
- Sarvam-M is the Hindi-first candidate, gated on tool-call validity.

### 6.3 Phase 0 gates (bench v2 on Ubuntu and Windows 11 + WSL2)

| Gate | Metric | Throughput (t12/t16) | Quality on vLLM (q-v) | Quality hybrid (q-h) |
|---|---|---|---|---|
| T | TTFT p95 at c=8 | ≤ 1.5 s | — | — |
| T | Decode per stream at c=8 | ≥ 30 tok/s | — | — |
| T / Q | 6-step agent median | ≤ 15 s | ≤ 25 s | ≤ 45 s (12 GB), ≤ 30 s (16 GB) |
| Q | TTFT p50 / p95 | — | ≤ 1 s / 2.5 s | ≤ 3 s / 6 s (12 GB), ≤ 2 s / 4 s (16 GB) |
| Q | Faithfulness; correctness | baseline recorded | ≥ 0.97; ≥ throughput | ≥ 0.97; ≥ q-v + 5 points |
| S | 2 h soak: OOM, crash, context overflow, host swap | 0 | 0 | 0 |
| S | Tool-call validity | ≥ 98% | ≥ 98% | ≥ 98% |
| S | Clean install to first answer (weights cached) | ≤ 10 min | ≤ 10 min | ≤ 15 min |

**Runs**
- Engine microbenchmarks with an `--n-cpu-moe` sweep.
- End-to-end ask over SSE, cold and warm.
- A 6-step agent scenario, recording prompt tokens per step and cache hits.
- Mixed load (80% ask, 20% agent) with a 500-document ingest running.
- A 2-hour soak.
- The golden set and 200 scripted tool calls.

**Reference hosts:** a 3060 with 32 GB DDR4 and a 5070 Ti with 32 GB DDR5. Repeat q16-h at 24 GB.

### 6.4 Inference work items

| # | Item | Files | Effort | Phase |
|---|---|---|---|---|
| 1 | Bench v2: SSE TTFT and inter-token latency, prefill and decode rates, concurrency sweep, agent scenario, mixed load, GPU and host samplers, report keyed by card, OS, engine, model and flags | `bench.py` | M | 0 |
| 2 | F2 agent context budget: count in tokens, cut on passage boundaries, compact older results to cite lines, only seen passages citable | `agents/runtime.py:319-367,446-453`, `llm.py:61-64`, `config.py` | S-M | 0 |
| 3 | F4 slot-aware admission shared across processes: capability record, static per-process partition plus the engine cap, per-profile timeouts | `llm.py:68-118`, `config.py:91-98`, `deploy/profiles/` | S-M | 0 |
| 4 | `ModelRoute` capability fields (`supports_tools`, `structured_output`, `prefix_cache`, `max_concurrency_hint`); callers branch on capability, never on engine name | `config.py:87-101`, `llm.py` | S | 0 |
| 5 | Profile files and installer RAM/WSL gate; `.wslconfig` guidance; GGUF files on the WSL ext4 volume | `deploy/profiles/`, `install.sh`, `install.ps1` | S | 0 |
| 6 | llama.cpp spike, then packaged as beta: env-driven entrypoint, `llm-hybrid` compose profile, image pinned by digest, internal network, `--api-key`, slot and props endpoints off, GGUF pinned by SHA-256 | `deploy/llamacpp-entrypoint.sh`, `compose.yaml` | S + S | 0 → 2 |
| 7 | F1 streaming tool loop (`Gateway.stream_tools`), streaming only verified sentences; async SSE endpoints | `llm.py:171-277`, `agents/runtime.py`, `api.py:277-297` | M | 1 |
| 8 | Reranker token budget (256 English / 384 Hindi, 30 candidates after the as-of second pass), parallel batches | `config.py:118`, `index.py:566-573` | S | 1 |
| 9 | Relation memoisation and batched amendments | `engine.py:629-630,710-757` | S | 1 |
| 10 | One encoder service (ONNX Runtime, CUDA or CPU int8 per profile) | `compose.yaml`, new `encoders/`, `index.py` | M | 1-2 |
| 11 | Shared answer cache in Postgres (tenant-keyed); LRU query-vector cache; one generation bump per ingest job | `ask.py`, `engine.py:171-199` | M | 2+ |
| 12 | Speculative decoding trials (ngram at low load, EAGLE-3 on t16); ship only on a measured gain | `deploy/vllm-entrypoint.sh` | S | 2+ |

## 7. Phased plan

The library choices for a zero-internet install, the air-gap bundle and the shipped skills and agents are in [offline-stack.md](offline-stack.md).

| # | Phase | Weeks | Goal | Must scope | Exit criteria |
|---|---|---|---|---|---|
| 0 | **Gate 0: safe and measured** | 1-3 | Close the public repo's known holes. Turn every GPU and quality claim into a measured number. | **Track A** (security and data-integrity release v0.2.0, about 2 engineer-weeks, ships first): G0-1…G0-22 below. **Track D** (bug sweep from the second audit, section 3.6). **Track B** (measurement): inference items 1-6. **Track C** (truth set): free verifier fixes; a messy real corpus of 200-500 docs (scanned, Hindi) with 50 golden questions; eval harness v1; clean-box installs on Windows and Ubuntu; Alembic baseline. | Hotfix tagged with security sign-off and its exit tests; bench table per card and profile; written engine decision; quality-profile go/no-go; real-GPU golden scores; install time measured |
| 1 | **Sovereign pilot kit** | 3-13 | One government/PSU site runs an air-gapped pilot on its own documents and passes IT review and an external security test (VAPT). | **Spine** M1-M10. **Hardening** H-1…H-10. **Content:** Hindi and English OCR with Docling (CPU); BGE-M3; NLI verifier; inline quote markup; F1 streaming; connector ACL interface on NTFS/AD folders; Oracle items 6-14. **Thin UX:** U1-U10. **Ops:** signed offline bundle with SBOM; backup, restore and upgrade; runbook; rollback flags. **Partner:** GeM listing, CERT-In-empanelled security test, STQC. | Security bar and pilot checklist all green (section 8); Critical and High security-test findings closed; a non-founder completes a USB dry-run with no internet |
| 2 | **Public launch** | 8-11 (launch about week 11) | Win attention on the moat, before DocsGPT ships its roadmap. | Trust benchmark; measured GPU numbers; quality profile as beta; Ollama, LM Studio and provider presets; U11-U15 (first-run flow, model card with Test, one chat home, copy buttons, demo GIF); signed GHCR images; docs site; hosted read-only demo; "Extend with MCP" guide; gpu-6gb marked community-supported. **Preconditions:** Gate 0 shipped, OIDC and the demo guard merged, bench numbers green. | 1k stars in 30 days (3k stretch); median install to first answer under 10 min; 20+ issues or PRs from non-founders; 1+ outside reproduction of the benchmark |
| 3 | **Pilot to reference** | 14-21 | Case study, letter of intent, second site | Pilot support with weekly golden-set reports; quality-profile soak on site; review queue; PDF citation highlight; rest of the Hindi UI; one government ACL source in production; performance items 9-11 as needed; systems-integrator partner track | Accuracy ≥ 85%, citation precision ≥ 95%, 0 wrong-version answers, 0 leaks; weekly users ≥ 30% of those invited; signed LOI or paid extension |
| 4 | **Enterprise table stakes** | 20-30 | Get onto Onyx and DocsGPT shortlists | SharePoint/OneDrive, Drive and Confluence with permission sync, in the leakage suite; SCIM; settings and RBAC UI; IA and mobile polish; Helm; multi-tenancy only if 2+ paying customers need it | 3 connectors at 0 leaks; one enterprise partner live; SharePoint onboarding under 30 min |
| 5 | **Ecosystem** | 24+ | Others extend Smriti without diluting the core | Plugin SDK on MCP and `EXTERNAL_PARSERS`, enforced by `authorize()`/`egress_policy()`; pack and skill gallery; second vertical on demand | 10+ community plugins or packs; 10+ contributors a month |

### 7.1 Gate 0 security and data-integrity release (Track A, about 2 engineer-weeks including tests)

**Status (4 November 2026): built as v0.2.0, not yet released.** All findings from both audits listed below are fixed, each with a regression test in `tests/test_audit_fixes.py` (132 tests pass). See `CHANGELOG.md` for the behaviour changes. One item is still open: signed artifact URLs to replace `?as=` (G0-2). The spine items (per-version access, OIDC, HMAC-keyed audit) remain Phase 1.

Every fix goes through a new `authz.py` with stub `authorize()` and `egress_policy()` functions, so the spine later replaces their insides instead of hunting for copies.

The second audit (section 3.6) added G0-14 to G0-22 and roughly doubled Track A. Order of work, following the second audit's recovery order:

1. **Stop data loss and overwrite:** G0-1, G0-14, G0-15, G0-16.
2. **Close access leaks:** G0-18, G0-3, G0-2, G0-8, G0-11, G0-19, G0-4, G0-5.
3. **Make ingest safe to retry:** G0-16 (reconcile), G0-22.
4. **Integrity and guardrails:** G0-17, G0-20, G0-21, G0-12, G0-13, plus the free verifier fixes from Track C.
5. **Remaining hardening:** G0-6, G0-7, G0-9, G0-10.
6. **Track D bug sweep** (section 3.6), running alongside Tracks B and C. Each fix lands with a regression test.

Steps 1-4 ship as v0.2.0, a release that blocks deployment. Steps 5-6 ship as v0.2.x patches.

| # | Fix | Files |
|---|---|---|
| G0-1 | Only curators (or the original uploader) can version an existing document. Access only tightens. Front-matter `doc_id`/`access`/`ref_no` is ignored without `curate`, and zip members follow the same rules. `doc_id` charset is restricted. | `engine.py:227-262,408-425,840-848`, `api.py:491-510` |
| G0-2 | Smriti refuses to start in demo mode off loopback unless explicitly overridden. The first-user fallback is removed, `?as=` is ignored outside demo mode, and demo mode shows a banner. Artifact URLs are signed. | `api.py:141-166`, `cli.py`, `config.py`, `web/` |
| G0-3 | Fixed widgets run as a low-privilege service principal, are `public`-only and rate-limited, and their output is buffered and filtered for classification and PII. | `channels.py`, `channels_api.py` |
| G0-4 | Tool `enabled` is enforced and tools are denied by default. The same check runs in preview, save and load. | `agents/__init__.py`, `agents/api.py`, `agents/runtime.py` |
| G0-5 | Injection-flagged evidence taints a run. A tainted run needs approval for every outbound tool. Approvals over API keys are refused. | `agents/runtime.py`, `agents/api.py` |
| G0-6 | Sandbox: token mandatory with constant-time comparison; kill by process group; local mode only on loopback for admins; a new `execute_code` grant. | `agents/sandbox.py`, `agents/tools.py`, `agents/workflow.py`, installers |
| G0-7 | Saaf pinned by SHA or vendored as a wheel; `uv.lock` committed; `--require-hashes`; images pinned by digest. | `pyproject.toml`, `Dockerfile`, `compose.yaml` |
| G0-8 | Tenant id in the answer cache and memory keys; tokens bound to a tenant; a two-tenant leakage test. | `ask.py`, `memory.py`, `config.py` |
| G0-9 | Upload size cap while streaming; zip limits (≤ 2,000 entries, ≤ 1 GB, ratio ≤ 100); expression step budget; regex timeout. | `api.py`, `engine.py`, `agents/expr.py` |
| G0-10 | Inline handlers removed; CSP and security headers added. | `web/app.js:468`, `api.py` |
| G0-11 | Grant checks on sources, policy, presets, deep health and metrics. | `api.py` |
| G0-12 | Approval compare-and-swap (`UPDATE … WHERE status='awaiting_approval'`). | `agents/runtime.py` |
| G0-13 | Stored PII restore map sealed and purged when the run ends. | `agents/runtime.py`, `credentials.py` |
| G0-14 | Front-matter `supersedes`/`amends` from non-pack sources becomes a *proposed* relation that a curator approves; it never retires a document on its own. | `engine.py:240,428-460`, `metadata.py` |
| G0-15 | Removal detection only runs after a discovery with no fetch errors. Tombstone after N consecutive misses (default 3) or a confirmed 404/410. Errored discovery marks the job `partial`, not `done`. | `connectors/__init__.py:350-440`, `connectors/web.py` |
| G0-16 | Ingest is retry-safe. Embed and index first, then write document, version and chunks in one transaction (or keep a `pending_index` state that a reconcile job finishes). `version_by_sha` matches only the current, live version of the same `doc_id`. | `engine.py:202-330`, `store.py:583` |
| G0-17 | Audit-chain stop-gap: per-tenant `pg_advisory_xact_lock` (SQLite: `BEGIN IMMEDIATE`) around read-prev-and-insert; `/api/stats` uses a cached verify result. The full HMAC design stays in M5. | `audit.py:33-51`, `api.py` (stats) |
| G0-18 | Separate `doc_class_rank` (unknown means most restricted) from `clearance_rank` (unknown means **deny**, i.e. public). Both compare case-insensitively. SSO claims are validated against `DOC_CLASSES`. | `models.py:18-23`, `access.py:54,120,161` |
| G0-19 | Pack-source crawler uses `egress.guarded_client` and `check_url` on every discovered item URL; streamed downloads with a byte cap. | `watch.py:120-140` |
| G0-20 | Escape `title`, `section`, `ref_no` and every other interpolated field in prompt tags (and scan them for injection). Chat `history` goes through the input guard and the PII masker. | `llm.py:395-420`, `ask.py`, `agents/runtime.py` |
| G0-21 | Malware scan fails closed by default (`SMRITI_MALWARE_FAIL_OPEN=1` to override); a clamd `ERROR` reply counts as a failure, never as clean; every scan failure is audited. | `engine.py:851-880` |
| G0-22 | Snapshot stores only successfully ingested keys; failed items retry on the next run; reaped dead jobs requeue once. | `watch.py:444`, `connectors/__init__.py:456-485` |

**Exit tests**
- v1 is unreadable after a hostile v2.
- Demo mode is refused on a non-loopback bind.
- A spoofed widget origin gets public content only.
- A viewer cannot preview an agent with a credentialed tool.
- An injected document followed by `read_url` asks for approval.
- Two concurrent approvals run the tool exactly once.
- A two-tenant cache test passes.
- A zip bomb is rejected.
- A deeply nested workflow expression is aborted.
- The CSP header is present.

**Rollback:** feature flags `SMRITI_AGENTS`, `SMRITI_CHANNELS` and `SMRITI_CODE_EXEC`.

### 7.2 Platform spine (Phase 1, weeks 3-10, Postgres only: no Redis, Temporal or separate auth service)

| # | Item | Effort |
|---|---|---|
| P4 | Alembic baseline, `smriti migrate`, version check at startup (done at the end of Phase 0) | S |
| M1 | Per-version access: `versions.access` column with backfill to the stricter label; reads check the version being served; loosening is an audited curator action | M |
| M2 | Identity port with OIDC (strict iss/aud/exp/nbf/signature/nonce, locally cached JWKS, PKCE, revocable server-side sessions), a trusted proxy only with mTLS or HMAC from allow-listed IPs, and tokens | L |
| M3 | Principal directory in the DB (principals, group members, service principals) fed by OIDC claims and LDAP sync; pack roles become role definitions | M |
| M4 | Central `authorize()` over documents, tools, channels, keys and share links; API keys with expiry and scopes | M |
| M5 | Audit chain serialised in the DB: advisory lock, `UNIQUE(tenant, prev_hash)`, HMAC key outside the DB, fail closed, daily anchor to WORM storage or syslog, scheduled verify | M |
| M6 | Coordination: scheduler leader lock and fire de-duplication; pack sources become seeded connectors (one job queue); DB row lock on versioning; rate limits in Postgres | M |
| M7 | Secret key from a Docker secret or OpenBao, MultiFernet with `smriti rotate-key`, no default Postgres password | S |
| M8 | Per-run sandbox isolation inside the existing container (UID pool, private 0700 directory, seccomp allow-list, kill by UID, wipe); optional gVisor for government installs; never `docker.sock` | M |
| M9 | `/api/v1` freeze: routers split out of `api.py`, Pydantic bodies, cursor pagination, one error format | M |
| M10 | Multi-process integration tests: 2 API workers plus the worker on Postgres | M |

Architecture rules for every later change:
- tenant id in every key;
- Alembic before any schema change;
- engines only behind the one OpenAI-compatible contract, chosen by declared capabilities;
- SQLite only for single-process community and demo installs.

### 7.3 Hardening track (Phase 1, in parallel, about 4-5 engineer-weeks)

| # | Item |
|---|---|
| H-1 | Per-run sandbox isolation (with M8) |
| H-2 | URL provenance: outbound tools may fetch only URLs from the question or earlier results; query length cap; separate agent egress allow-list |
| H-3 | Pinned-IP HTTP transport against DNS rebinding; normalise IPv4-mapped addresses; Smriti's own services on the deny list; agent tools default to no private networks; Squid egress-proxy recipe |
| H-4 | Shared links withhold tool-result content the viewer may not use; 7-day expiry; revocation; every view audited |
| H-5 | API key scopes, expiry, optional IP binding; keys act only as service principals |
| H-6 | SQL tool: read-only role checked at save time; per-dialect statement timeouts |
| H-7 | Secrets and encrypted backups (with M7) |
| H-8 | Hidden titles redacted in the role simulator; hashed tokens with constant-time comparison |
| H-9 | CycloneDX SBOM, cosign-signed images, offline bundle with SHA-256 manifest, `pip-audit` and `trivy` in CI |
| H-10 | Rate limits and per-user token budgets in Postgres |

### 7.4 Answer quality work (owner: Oracle's plan)

| # | Item | Phase |
|---|---|---|
| 1-4 | Verifier tokenizer keeps negation and quantifiers (English and Hindi); Devanagari digits and dates normalised; per-sentence drop or flag instead of the 60% average; number + unit + noun binding and a polarity check; polarity in the semantic cache key | 0 |
| 5 | Eval harness v1: real endpoints, slices, per-slice gates, verifier pair suite (~200), expected claims, run keys | 0 |
| 6-7 | Inline quote markup (`[n]` plus a verbatim span), streamed sentence by sentence; mDeBERTa-xnli NLI on CPU int8 (GPU optional on 16 GB) | 1 |
| 8-9 | Agent context budget, search before the first model call, richer tool descriptions; research agent that plans from retrieval results, runs sub-questions in parallel and re-plans once (max 3 sub-questions on the quality profile) | 1 |
| 10-14 | BGE-M3 dense+sparse replaces Qwen3-Embedding; Hindi suffix stripper and route patterns; as-of second-pass search; reranker cap; Qdrant/local parity; token counts from the engine | 1 |
| 15-16 | Small route classifier; contextual retrieval (only if it adds 3+ points of recall) | 2-3 |

**Hindi query translation stays off by default.** It is switched on per tenant (`retrieval.query_translation: off|auto|on`) only if Hindi→English recall@10 is below 0.85, or Hindi abstention runs more than 10 points above English.

When on, it works as follows:
- It first expands queries with the glossary.
- It runs as an extra retrieval leg fused by RRF, never as a replacement for the original query.
- It translates the masked query and re-runs the guard on the translation.
- It uses a local model only.
- The translated form goes into the cache key and the audit log.
- It stays on only if it adds at least 3 points of recall.

**Eval gates**

| Metric | Throughput | Quality |
|---|---|---|
| Leakage / red-team success | 0 / 0 | 0 / 0 |
| recall@10, overall and per slice (Hindi, cross-lingual, as-of) | ≥ 0.88 overall, ≥ 0.85 per slice | ≥ 0.90 overall, ≥ 0.85 per slice |
| Faithfulness of shown sentences | ≥ 0.95 | ≥ 0.97 |
| Claim recall | ≥ 0.75 | ≥ 0.85, and ≥ throughput on every slice |
| False abstention | ≤ 15% | ≤ 10% |
| Citation precision / quote validity | ≥ 0.85 / ≥ 0.98 | ≥ 0.90 / ≥ 0.98 |
| Agent task success / tool-call validity | ≥ 0.75 / ≥ 0.97 | ≥ 0.85 / ≥ 0.99 |
| Any slice regressing more than 2 points | blocks | blocks |

**Golden set**
- At least 150 cases per tenant at pilot and 300 at general availability.
- Slices: by route; 50% English, 30% Hindi, 20% cross-lingual; 15% as-of; 15% must-abstain; 10% permission; 10% scanned.

**Cadence**
- CI runs fake-model suites.
- Nightly runs use real models on two reference profiles.
- Weekly runs cover every profile.

The judge model must reach κ ≥ 0.7 against human labels before it can gate. It never judges permissions.

### 7.5 UX work (owner: Advocate's plan)

| Phase | Items |
|---|---|
| 0 (front end only, can start early) | U1 accessibility baseline (real buttons, focus-visible, citations as buttons, drawer as a dialog, announced toasts); U5 plain errors and a model-offline banner; U6 evidence shows sources first |
| 1 (about 2.5 weeks) | U2 Hindi strings and a bundled Devanagari font on officer screens; U3 sign-in page with demo mode labelled; U4 Ask history that survives a reload; U7 access pickers on upload; U8 readable approvals plus `GET /api/runs?status=awaiting_approval` and a nav badge; U9 golden-set authoring form; U10 readability on officer screens; minimal admin console |
| 2 (about 2 weeks) | U11 first-run screen (installer opens the browser; demo data or first source); U12 model card with Test; U13 one chat home (Ask becomes the default agent, with an agent picker); U14 copy buttons for keys and share links; U15 demo GIF recorded after the fixes |
| 4 | Full wizard, settings UI, navigation reshuffle, mobile, file drop in chat, workflow outline editor |

**UX exit checks**
- axe scan clean on Ask, chat, citations and approvals.
- The Hindi citation check works keyboard-only.
- No untranslated Hindi strings.
- The offline banner appears within 60 s.
- Install timed on clean machines by a non-founder.

## 8. Government pilot bar

### 8.1 Security bar

- [ ] Gate 0 merged, with exit tests green in CI.
- [ ] OIDC against the on-prem IdP, or a trusted proxy with mTLS. Demo mode is impossible on a network interface. Deprovisioned users lose access within 15 minutes.
- [ ] Per-version access stored and checked. Leakage evals (cross-user, cross-tenant, cross-version, share link, channel) show **0 leaks**.
- [ ] Audit chain serialised in the DB and HMAC-keyed. `verify` passes with 2+ API workers plus the job worker. Daily anchor. Logs kept ≥ 180 days (CERT-In).
- [ ] Tools denied by default by role. Approval required for outbound calls once tainted, with URL provenance. No approvals over API keys.
- [ ] Code execution behind a grant with per-run isolation (gVisor where possible), or switched off for the pilot.
- [ ] No fixed channel identity above `public`. Classification and PII filters on every channel.
- [ ] Agent egress blocks private networks. Pinned-IP transport. Every compose service on the deny list.
- [ ] Secrets from a Docker secret or OpenBao. No default passwords. Rotation tested. Encrypted backups.
- [ ] Dependencies pinned by hash, images by digest, model weights by SHA-256. SBOM, signed images, offline bundle. No `:latest`.
- [ ] CSP with no inline script. Upload and zip caps.
- [ ] Feature flags for agents, channels and code execution, with documented rollback.
- [ ] External security test (VAPT) by a CERT-In-empanelled auditor; Critical and High findings closed. Fix SLA written: Critical within 72 h, High within 14 days.

### 8.2 Pilot readiness checklist

- [ ] Offline install on a clean Ubuntu box with one 12 or 16 GB card and no internet, in under 60 minutes.
- [ ] Sign-in through the site's IdP, with roles mapped from groups.
- [ ] Scanned Hindi and English PDFs ingest with OCR and layout. Low-confidence pages are queued.
- [ ] 50+ customer-authored golden questions scored on the site's own card: 0 leaks, 0 wrong-version answers, red team passed, Hindi slice within 10 points of English.
- [ ] The default profile for the site's card passed the Phase 0 gates. The quality profile is labelled beta.
- [ ] Backup, restore and upgrade (N→N+1) tested without data loss.
- [ ] The audit chain verifies with multiple workers, is anchored, and exports in a format auditors accept.
- [ ] Accessibility: no critical axe issues; keyboard-only walkthrough passes.
- [ ] Hindi officer screens reviewed by a real user from the site.
- [ ] Runbook, log-bundle command, a measured sizing sheet (users per card), and a clean model licence register.

## 9. Metrics and risks

| Phase | Primary metric | Supporting |
|---|---|---|
| 0 | Engine decision and gates passed per card | Golden accuracy; citation precision; abstention on answerable questions ≤ 20%; model-answer acceptance ≥ 60%; install time; hotfix shipped |
| 1 | Security bar and pilot checklist all green | Offline install time; OCR confidence; 0 open Critical/High findings; non-founder dry run |
| 2 | Stars at 30 days (1k; stretch 3k) | Install p50 < 10 min; 20+ non-founder issues or PRs; benchmark reproductions; Ollama share of installs |
| 3 | Pilot score bar plus LOI or paid extension | Weekly users ≥ 30% of invited; abstention trend; quality-profile soak with 0 incidents |
| 4 | One enterprise partner live | Connector leaks 0; SharePoint onboarding < 30 min |
| 5 | 10+ community plugins or packs | 10+ contributors a month |

| # | Risk | Test that retires it | If it fails |
|---|---|---|---|
| R1 | A 30B MoE with offload is too slow for agents on 12 GB | Phase 0 agent scenario: median ≤ 45 s, TTFT p95 ≤ 6 s | 12 GB ships a model that fits on the GPU; the headline becomes "verified answers on 12 GB" |
| R2 | The verifier refuses too many answerable questions on real documents | 50-question real-corpus set after the verifier fixes: abstention ≤ 20% | Pause features; tune retrieval and the verifier |
| R3 | Small-model answers fail verification too often | Model-answer acceptance ≥ 60% | Move 12 GB to 8B; tune prompts and quote markup |
| R4 | Launching a "trust" product with exploitable holes | Gate 0 before any public post; external security test before the pilot | No launch until signed off |
| R5 | Two engines exceed one founder's capacity | One default per card; CI smoke test; the other engine marked "advanced" | Drop the non-default engine from the installer |
| R6 | Pilot documents are mostly scanned or Hindi | Sample 100 of the target site's circulars in week 1 | OCR stays first in Phase 1 |
| R7 | Phase 1 overruns | Hire by week 4; weekly burn-down | Launch holds at week 11; pilot moves to about week 16-17; the security bar is never cut |
| R8 | The community cares about UX more than verification | Post benchmark findings on r/LocalLLaMA in week 9 | Lead with "Ollama-easy plus small-GPU numbers", with trust second |
| R9 | DocsGPT ships SSO and connectors first | Benchmark launch by week 11 | Compete on verification, versions and India-first PII, not connector count |

## 10. Decisions and how tensions were resolved

| Tension | Resolution | Why |
|---|---|---|
| Platform spine first vs security fixes first (Architect, Skeptic) | A one-week Gate 0 hotfix release first, routed through stub `authorize()`/`egress_policy()`; then the Postgres-only spine and a parallel hardening track | Several holes are exploitable now and cost days to close; the stubs keep the spine from becoming a hunt for copies |
| Prove depth vs fix the UX shell (Strategist, Advocate) | A thin UX layer (accessibility, Hindi officer screens, sign-in, history, access pickers, readable approvals) inside Phase 1; polish waits for Phase 4 | Government IT review checks accessibility and Hindi, and pilot adoption depends on the officer's first session |
| Must-match gaps vs breadth freeze (Scout, Strategist) | OIDC/LDAP, OCR, minimal admin and the ACL interface before the pilot; Ollama presets before launch; SaaS connectors, SCIM, multi-tenancy and the SDK after pilot signal, with explicit competitor triggers | Breadth is the crowded market; the moat is uncontested |
| Two engines vs one founder (Tuner, Strategist) | vLLM as the only default on day one; llama.cpp MoE as an opt-in beta; per-card flip rules from Phase 3; one engine per install | Keeps support and the nightly eval matrix small while keeping the large-model path |
| Richer verifier vs VRAM budget (Oracle, Tuner) | NLI on CPU int8, per streamed sentence; BGE-M3 replaces the current embedder; one encoder process; inline quotes instead of JSON-constrained answers; multivector deferred | Fits the 12 GB budget and keeps answers streaming |
| Hindi query translation | Off by default; switched on per tenant only when the Hindi eval slices demand it, as an extra fused retrieval leg | BGE-M3 is cross-lingual; translation adds latency and an injection path |

**Still deferred until pilot signal:**
- SaaS connectors and SCIM;
- runtime multi-tenancy;
- the plugin SDK and gallery;
- more chat channels and workflow-canvas nodes;
- Kubernetes;
- KTransformers and SGLang;
- speech-to-text;
- a paid-edition split (never, for trust features).
