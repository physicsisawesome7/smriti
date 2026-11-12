# Evaluation: proving a change is better

Smriti has two kinds of test set.

| Set | What it is for | Command |
|---|---|---|
| **Golden set** of a tenant pack | A release gate: nothing ships if it breaks, leaks or lets an attack through | `smriti eval gate` |
| **Hard set** | Choosing between configurations: models, retrieval settings, prompts | `smriti eval hard`, `smriti eval compare` |

The demo pack's golden set has 14 questions over 14 documents, and every
embedder scores 14/14 on it, including one with no neural model. It catches
regressions. It cannot tell you whether a change is an improvement. The hard
set can.

## The hard set

A synthetic corpus and question set, generated from a seed
(`src/smriti/hardset.py`), so every run is reproducible and nothing is copied
from a real organisation.

- **268 documents** in three generic domains: purchasing and contracts, staff
  policy, equipment maintenance. Many are near-duplicates that differ only in
  their numbers (the same policy at two sites, sibling machine models), 12 are
  written in Hindi, 25 are restricted.
- **190 questions** in eight slices:

| Slice | Questions | What it tests |
|---|---|---|
| exact | 26 | The question uses the document's identifiers and wording |
| paraphrase | 38 | The question avoids the document's wording and names things differently ("lab supplies" for "laboratory consumables") |
| cross-language | 29 | Hindi and Hinglish questions over English documents; English questions over Hindi documents |
| multi-hop | 16 | Two documents are needed (a contract, then the policy or approval limit that governs it) |
| table | 15 | One cell of a table, or arithmetic over its rows |
| superseded | 28 | A clause was amended: the rule today, and the rule as of an earlier date |
| unanswerable | 15 | Nothing in the library answers it; Smriti must abstain |
| restricted | 23 | The asker may not see the document (must not be retrieved), or may (must be found) |

```bash
smriti eval hard                       # build, index and score with the current settings
smriti eval hard --out ./hardset       # also keep the generated pack to read
smriti eval hard --seed 8              # the same shape with other facts
```

It prints, per slice: pass rate (the whole case: retrieval, answer text,
abstention, no leak), recall@10, MRR, answer correctness, abstention accuracy
and leaks. It exits 1 on any leak, or if `exact`, `superseded` or `restricted`
fall below their floors (0.9, 0.9, 1.0); those must hold on every
configuration. The other slices are where configurations differ.

## Comparing two configurations

A configuration is a name and environment settings:
`NAME:KEY=VALUE,KEY=VALUE` (`KEY=` clears a variable; a bare name means the
environment as it is).

```bash
smriti eval compare \
  "hash:SMRITI_EMBED_URL=,SMRITI_CHAT_URL=,SMRITI_RERANK_URL=" \
  "e5:SMRITI_EMBED_URL=http://localhost:8080/v1,SMRITI_EMBED_MODEL=intfloat/multilingual-e5-small,SMRITI_CHAT_URL=,SMRITI_RERANK_URL=" \
  --json compare.json
```

Both runs index the same corpus from scratch in a temporary store (never the
live database or vector store) and answer the same questions. The comparison
is paired, on two measures: **pass** (the whole case) and **retrieved** (an
expected document in the top 10). For each slice it gives both rates, the
difference, and a 95% bootstrap interval; `*` marks a difference whose
interval excludes zero. The JSON lists which questions were gained and lost.

To compare on your own documents, pass `--pack DIR`: the pack's golden set is
scored instead of the hard set.

Inside the Docker stack, where the model servers are reachable:

```bash
docker compose run --rm --no-deps --entrypoint smriti smriti-api eval compare \
  "no-llm:SMRITI_CHAT_URL=" "llm" --json /data/compare.json
```

## A test set from your own documents

```bash
smriti eval build --as km --count 60 --out golden-candidates.yaml
```

Samples passages the named person may read and drafts one question each.
With a chat model the question is in the model's own words and its answer
must be a phrase copied from the passage; drafts that invent an answer or
give it away in the question are rejected. Without a model the passage's
sentence is used with the fact blanked out, marked `drafted_by: rules`,
because it still needs rewording. Each candidate records whether Smriti
answers it correctly today (`passes_now`).

A curator reviews the file, fixes wording, deletes what is wrong or trivial,
and moves accepted cases into the pack's `evals/golden.yaml`. Candidates are
never added to the golden set automatically: a test set nobody reviewed
proves nothing.

## Baseline (12 November 2026)

Measured on the reference machine (RTX 4050 6 GB, `gpu-6gb` profile). No
reranker in any run.

| Slice | Hash embedder, no model | e5-small, no model | e5-small + Qwen3-4B |
|---|---|---|---|
| exact | 1.00 | 1.00 | 1.00 |
| paraphrase | 0.00 | 0.00 | 0.18 |
| cross-language | 0.14 | 0.14 | 0.14 |
| multi-hop | 0.00 | 0.00 | 0.00 |
| table | 0.53 | 0.53 | 0.53 |
| superseded | 0.96 | 0.96 | 0.96 |
| unanswerable | 0.67 | 0.67 | 0.67 |
| restricted | 1.00 | 1.00 | 1.00 |
| **all (190)** | **0.52** | **0.52** | **0.55** |
| leaks | 0 | 0 | 0 |
| index time | 9 s | 39 to 52 s | 51 s |
| per question | 38 ms | 49 to 55 ms | 906 ms |

Retrieval alone (expected document in the top 10):

| Slice | Hash | e5-small |
|---|---|---|
| paraphrase | 0.37 | 0.66 |
| cross-language | 0.14 | 0.24 |
| multi-hop | 0.00 | 0.00 |
| all with an expected document (162) | 0.60 | 0.69 |

What the baseline says:

1. **What Smriti is built on holds.** Lookups by identifier, amendment chains
   with as-of dates, and permissions pass on every configuration, with zero
   leaks across 190 questions.
2. **Paraphrased questions are found but not answered.** e5-small retrieves the
   right document for 66% of them, yet only 18% pass even with the chat model,
   because Smriti abstains before the model is asked: the top passage's score
   stays below the answering threshold (`retrieval.min_support`) for reworded
   questions when no reranker is configured, as on the 6 GB profile. This is
   the largest single gap.
3. **Cross-language retrieval is weak** (24% with e5-small) on a corpus with
   near-duplicate documents, far below the 4/4 of the ten-question probe on 39
   chunks. Small corpora flatter retrievers.
4. **Multi-hop questions fail entirely.** A single search cannot find the
   second document; this needs the corrective loop or an agent.
5. **Table arithmetic fails.** All eight single-cell lookups pass; none of the
   seven totals over a Markdown table is computed.
6. **One in three unanswerable questions gets an answer** from a near-miss
   document.

Items 2 to 6 are the work list for Phase C in [plan-next.md](plan-next.md).
Each change there is accepted only if `smriti eval compare` shows a gain with
no slice lost.

## Limits of the hard set

- It is synthetic and templated. Real questions are messier; it complements a
  reviewed golden set from your own documents, it does not replace one.
- Answer correctness is a string match on the key fact, not a judgement of the
  whole answer.
- 15 to 38 questions per slice give wide intervals; a difference of a few
  points in one slice is noise. That is why `compare` prints intervals.
