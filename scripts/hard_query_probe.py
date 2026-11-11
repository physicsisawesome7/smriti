"""Hard-query probe: dense vs BM25 vs hybrid (RRF) per embedder, on paraphrased
and cross-language questions whose wording avoids the documents' own terms.

Run inside the Compose network (see docs/model-benchmarks.md); it indexes the
demo pack itself and writes /out/probe.json. Edit MODELS for the servers you run."""

import json
import math
import os
import pathlib
import sys
import tempfile
import time
from collections import Counter

import httpx

sys.path.insert(0, "/bench/src")
from smriti.index import tokenize


def export_chunks():
    """Index the pack with the built-in models and return its chunks."""
    os.environ.update({"SMRITI_EMBED_URL": "", "SMRITI_CHAT_URL": "", "SMRITI_RERANK_URL": ""})
    from smriti.api import Platform
    from smriti.cli import seed
    from smriti.config import Settings

    tmp = pathlib.Path(tempfile.mkdtemp())
    url = f"sqlite:///{(tmp / 's.db').as_posix()}"
    plat = Platform(Settings.from_env(data_dir=tmp, database_url=url, scheduler=False, auth_mode="demo"))
    seed(plat)
    t = plat.tenant(None)
    return [
        {"doc": d["doc_id"], "header": c.get("header") or "", "text": c["text"]}
        for d in t.store.list_documents(t.id)
        for c in t.store.doc_chunks(t.id, d["doc_id"])
    ]


chunks = export_chunks()
texts = [(c["header"] + "\n" + c["text"]).strip() for c in chunks]
PG = {"ref-gcc-2022", "ref-gcc-2022-cs-07", "ref-gcc-2022-cs-08"}
Q = [
    ("en-para", "How long does a firm get to hand in its bank guarantee after it wins the work?", PG),
    ("hi-x", "काम मिलने के बाद ठेकेदार को बैंक गारंटी कितने दिनों में जमा करनी होती है?", PG),
    (
        "en-para",
        "Who is allowed to sign off on a construction contract worth 300 crore rupees?",
        {"ref-brd-2024-w1-poa"},
    ),
    ("en-para", "What should be inspected on bridges and embankments before the heavy rains?", {"ref-nr-eng-2025-11"}),
    ("hi-x", "बारिश से पहले पुलों की जाँच के क्या निर्देश हैं?", {"ref-nr-eng-2025-11"}),
    ("en-x", "What precautions must workers take while working on the railway line?", {"ref-nr-safety-2025-04"}),
    ("en-para", "What is the penalty if the project is finished late?", {"ref-gcc-2022"}),
    ("en-para", "How much earnest money has to go in with a bid for the doubling works?", {"ref-nr-dli-eng-2025-14"}),
    ("en-para", "What has to be done before the award letter is sent to the successful firm?", {"ref-cao-c-sop-07"}),
    ("hi-x", "रामपुर गाँव में किन भूखंडों का अधिग्रहण हो रहा है?", {"ref-la-p-17-sch-2025"}),
]
MODELS = [
    ("multilingual-e5-small", "http://embed:80/v1", "intfloat/multilingual-e5-small", "query: ", "passage: "),
    (
        "Qwen3-Embedding-0.6B",
        "http://bench-embed-qwen3:80/v1",
        "Qwen/Qwen3-Embedding-0.6B",
        "Instruct: Given a question, retrieve passages that answer it\nQuery: ",
        "",
    ),
]


def embed(url, model, items):
    out = []
    for i in range(0, len(items), 8):
        r = httpx.post(f"{url}/embeddings", json={"model": model, "input": items[i : i + 8]}, timeout=900)
        r.raise_for_status()
        out += [d["embedding"] for d in r.json()["data"]]
    return [[x / (math.sqrt(sum(y * y for y in v)) or 1) for x in v] for v in out]


# BM25
docs_tok = [tokenize(t) for t in texts]
N = len(docs_tok)
avg = sum(map(len, docs_tok)) / N
df = Counter(w for d in docs_tok for w in set(d))


def bm25(q, k1=1.2, b=0.75):
    qt = tokenize(q)
    sc = []
    for d in docs_tok:
        tf = Counter(d)
        s = 0.0
        for w in qt:
            if w in tf:
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(d) / avg))
        sc.append(s)
    return sc


def rank(scores):
    return sorted(range(len(scores)), key=lambda i: -scores[i])


def rrf(*ranks, k=60):
    f = Counter()
    for r in ranks:
        for pos, i in enumerate(r):
            f[i] += 1 / (k + pos + 1)
    return [i for i, _ in f.most_common()]


def hit(order, gold, n):
    return any(chunks[i]["doc"] in gold for i in order[:n])


def first(order, gold):
    return next((p + 1 for p, i in enumerate(order) if chunks[i]["doc"] in gold), None)


results = {}
K = 20  # as in Smriti: top-k of each list; BM25 lists only chunks that match a query term
bm_orders = [[i for i in rank(sc) if sc[i] > 0][:K] for sc in (bm25(q) for _, q, _ in Q)]
results["BM25 only"] = bm_orders
for name, url, model, qp, dp in MODELS:
    t0 = time.perf_counter()
    dv = embed(url, model, [dp + t for t in texts])
    qv = embed(url, model, [qp + q for _, q, _ in Q])
    secs = time.perf_counter() - t0
    dense = [rank([sum(a * b for a, b in zip(q, d, strict=False)) for d in dv]) for q in qv]
    results[f"{name} dense"] = dense
    results[f"{name} + BM25 (RRF)"] = [rrf(d[:K], b) for d, b in zip(dense, bm_orders, strict=False)]
    print(f"# {name}: embedded {len(texts)} chunks + {len(Q)} queries in {secs:.1f}s", flush=True)

rows = []
for label, orders in results.items():
    h1 = sum(hit(o, g, 1) for o, (_, _, g) in zip(orders, Q, strict=False))
    h5 = sum(hit(o, g, 5) for o, (_, _, g) in zip(orders, Q, strict=False))
    mrr = sum(1 / (first(o, g) or 1e9) for o, (_, _, g) in zip(orders, Q, strict=False)) / len(Q)
    xl = [i for i, (k, _, _) in enumerate(Q) if k != "en-para"]
    x5 = sum(hit(orders[i], Q[i][2], 5) for i in xl)
    rows.append({"setup": label, "hit@1": h1, "hit@5": h5, "mrr": round(mrr, 2), "cross_lang_hit@5": f"{x5}/{len(xl)}"})
for r in rows:
    print(r)
pathlib.Path("/out/probe.json").write_text(
    json.dumps({"queries": [q for _, q, _ in Q], "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
)
