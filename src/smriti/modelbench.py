"""Benchmark model servers directly: compare models and tune throughput.

``smriti bench`` load-tests the whole Smriti API. This module measures the
model servers underneath it, so you can pick a model and tune the server
before you put Smriti in front of it. Four modes:

    # LLM: sweep concurrency, report TTFT, inter-token latency, tokens/s, goodput
    smriti modelbench chat --target http://127.0.0.1:8001/v1,smriti-chat,qwen3-4b \\
        --target http://127.0.0.1:8002/v1,smriti-chat,qwen3-8b \\
        --concurrency 1,2,4,8,16 --prompt-tokens 1500 --max-tokens 256

    # Embedding servers (TEI, vLLM, Ollama: any OpenAI /embeddings)
    smriti modelbench embed --target http://embed:80/v1,intfloat/multilingual-e5-small --batch 1,8,32

    # Rerankers (TEI /rerank)
    smriti modelbench rerank --target http://rerank:80,BAAI/bge-reranker-v2-m3 --candidates 10,30,50

    # Retrieval quality: every embedder x reranker combination on the tenant's golden set
    smriti modelbench retrieval --embed hash --embed http://embed:80/v1,intfloat/multilingual-e5-small,"query: ","passage: " \\
        --rerank none --rerank http://rerank:80,BAAI/bge-reranker-v2-m3

Each mode prints a table and can write the full result with ``--json``. Model
servers are only ever called at the URLs you pass; nothing goes to the internet.

Definitions (as in vLLM's and MLPerf's serving benchmarks):

* **TTFT**: time from sending the request to the first generated token.
* **ITL**: gap between consecutive streamed tokens after the first.
* **TPOT**: (end-to-end latency - TTFT) / (output tokens - 1), per request.
* **Goodput**: requests per second that met *both* latency targets
  (``--slo-ttft-ms`` and ``--slo-tpot-ms``). Throughput that misses the
  target is not useful throughput.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------- helpers


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def parse_target(spec: str) -> dict[str, Any]:
    """``URL,MODEL[,NAME]`` -> {url, model, name}."""
    parts = [p.strip() for p in spec.split(",")]
    if not parts[0].startswith(("http://", "https://")):
        raise ValueError(f"target must start with a URL: {spec!r}")
    model = parts[1] if len(parts) > 1 and parts[1] else ""
    return {"url": parts[0].rstrip("/"), "model": model, "name": parts[2] if len(parts) > 2 else (model or parts[0])}


def ints(s: str) -> list[int]:
    return [int(x) for x in str(s).split(",") if x.strip()]


_CORPUS: list[str] | None = None


def corpus_paragraphs() -> list[str]:
    """Real paragraphs from the demo pack, so prompts tokenize like real
    documents (clause numbers, amounts, Hindi) rather than lorem ipsum."""
    global _CORPUS
    if _CORPUS is None:
        root = Path(__file__).resolve().parents[2] / "packs" / "tenants" / "railways-demo" / "corpus"
        paras: list[str] = []
        for f in sorted(root.glob("*.md")) if root.exists() else []:
            paras += [p.strip() for p in f.read_text(encoding="utf-8").split("\n\n") if len(p.split()) >= 8]
        _CORPUS = paras or [
            "The contractor shall submit the performance guarantee within 21 days of the letter of acceptance "
            "in the form of a bank guarantee for five per cent of the contract value."
        ]
    return _CORPUS


def text_of_tokens(n_tokens: int, rng: random.Random, words_per_token: float = 0.75) -> str:
    """About ``n_tokens`` tokens of document text. 0.75 words per token holds
    for English prose; clause numbers, amounts and Hindi need far more tokens
    per word, so ``run_chat`` measures the real ratio on the server first."""
    paras = corpus_paragraphs()
    out: list[str] = []
    words = 0
    target = max(1, int(n_tokens * words_per_token))
    while words < target:
        p = paras[rng.randrange(len(paras))]
        out.append(p)
        words += len(p.split())
    return "\n\n".join(out)


def table(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> str:
    head = [h for _, h in cols]
    body = [[str(r.get(k, "")) for k, _ in cols] for r in rows]
    w = [max(len(h), *(len(b[i]) for b in body)) if body else len(h) for i, h in enumerate(head)]
    line = lambda cells: "  ".join(c.rjust(w[i]) if i else c.ljust(w[i]) for i, c in enumerate(cells))  # noqa: E731
    return "\n".join([line(head), line(["-" * x for x in w]), *(line(b) for b in body)])


def _write(path: str | None, data: Any) -> None:
    if path:
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
        print(f"\nwrote {path}")


# ---------------------------------------------------------------- chat


QUESTION = (
    "Using only the passages above, answer in at most five sentences and cite the clause numbers: "
    "what must the contractor do after the letter of acceptance, and by when?"
)


def build_chat_prompt(
    prompt_tokens: int, shared_prefix: bool, rng: random.Random, words_per_token: float = 0.75
) -> list[dict[str, str]]:
    """A RAG-shaped prompt: system rules, retrieved passages, then the question.
    With ``shared_prefix`` every request carries the same passages (best case
    for prefix caching); otherwise a nonce at the start makes every prompt
    unique (worst case)."""
    system = "You answer from the passages given. Cite the passage for every fact. If unsure, say so."
    ctx = text_of_tokens(prompt_tokens, random.Random(7) if shared_prefix else rng, words_per_token)
    if not shared_prefix:
        system = f"[req {rng.getrandbits(48):012x}] " + system
    return [{"role": "system", "content": system}, {"role": "user", "content": f"Passages:\n{ctx}\n\n{QUESTION}"}]


def stream_once(
    client: httpx.Client, target: dict[str, Any], messages: list[dict[str, str]], max_tokens: int, extra: dict[str, Any]
) -> dict[str, Any]:
    body = {
        "model": target["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        **extra,
    }
    t0 = time.perf_counter()
    stamps: list[float] = []
    usage: dict[str, Any] = {}
    with client.stream("POST", f"{target['url']}/chat/completions", json=body) as r:
        if r.status_code >= 400:
            r.read()
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content") or d.get("reasoning_content") or d.get("reasoning"):
                    stamps.append(time.perf_counter())
    end = time.perf_counter()
    if not stamps:
        raise RuntimeError("the server streamed no tokens")
    out_tokens = int(usage.get("completion_tokens") or len(stamps))
    ttft = stamps[0] - t0
    e2e = end - t0
    return {
        "ttft": ttft,
        "e2e": e2e,
        "itl": [b - a for a, b in zip(stamps, stamps[1:], strict=False)],
        "tpot": (e2e - ttft) / (out_tokens - 1) if out_tokens > 1 else 0.0,
        "out_tokens": out_tokens,
        "in_tokens": int(usage.get("prompt_tokens") or 0),
    }


def chat_level(
    target: dict[str, Any],
    concurrency: int,
    requests: int,
    prompt_tokens: int,
    max_tokens: int,
    shared_prefix: bool,
    slo_ttft_ms: float,
    slo_tpot_ms: float,
    extra: dict[str, Any],
    seed: int = 0,
    words_per_token: float = 0.75,
) -> dict[str, Any]:
    rng = random.Random(seed + concurrency)
    prompts = [build_chat_prompt(prompt_tokens, shared_prefix, rng, words_per_token) for _ in range(requests)]
    client = httpx.Client(timeout=600, limits=httpx.Limits(max_connections=concurrency + 4))
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    def one(i: int) -> None:
        try:
            results.append(stream_once(client, target, prompts[i], max_tokens, extra))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}"[:400])

    t0 = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as ex:
        list(ex.map(one, range(requests)))
    wall = time.perf_counter() - t0
    client.close()

    ttft = [r["ttft"] * 1000 for r in results]
    tpot = [r["tpot"] * 1000 for r in results]
    itl = [g * 1000 for r in results for g in r["itl"]]
    e2e = [r["e2e"] for r in results]
    out_tok = sum(r["out_tokens"] for r in results)
    in_tok = sum(r["in_tokens"] for r in results)
    good = sum(1 for r in results if r["ttft"] * 1000 <= slo_ttft_ms and r["tpot"] * 1000 <= slo_tpot_ms)
    return {
        "target": target["name"],
        "concurrency": concurrency,
        "ok": len(results),
        "errors": len(errors),
        "error_sample": errors[:3],
        "wall_s": round(wall, 2),
        "req_per_s": round(len(results) / wall, 3) if wall else 0,
        "out_tok_per_s": round(out_tok / wall, 1) if wall else 0,
        "in_tok_per_s": round(in_tok / wall, 1) if wall else 0,
        "mean_in_tokens": round(in_tok / len(results)) if results else 0,
        "mean_out_tokens": round(out_tok / len(results)) if results else 0,
        "ttft_ms_p50": round(pct(ttft, 50)),
        "ttft_ms_p95": round(pct(ttft, 95)),
        "ttft_ms_p99": round(pct(ttft, 99)),
        "tpot_ms_p50": round(pct(tpot, 50), 1),
        "tpot_ms_p95": round(pct(tpot, 95), 1),
        "itl_ms_p50": round(pct(itl, 50), 1),
        "itl_ms_p95": round(pct(itl, 95), 1),
        "e2e_s_p50": round(pct(e2e, 50), 2),
        "e2e_s_p95": round(pct(e2e, 95), 2),
        "decode_tok_per_s_per_req": round(1000 / pct(tpot, 50), 1) if tpot and pct(tpot, 50) else 0,
        "goodput_req_per_s": round(good / wall, 3) if wall else 0,
        "slo_attainment": round(good / requests, 3) if requests else 0,
    }


def summarise_chat(levels: list[dict[str, Any]], min_attainment: float = 0.9) -> dict[str, Any]:
    """Per target: the peak throughput, and the highest concurrency that still
    meets the latency targets for ``min_attainment`` of requests (the knee)."""
    out: dict[str, Any] = {}
    for name in dict.fromkeys(lv["target"] for lv in levels):
        mine = [lv for lv in levels if lv["target"] == name and lv["ok"]]
        if not mine:
            out[name] = {"peak_out_tok_per_s": 0, "max_concurrency_within_slo": None}
            continue
        peak = max(mine, key=lambda lv: lv["out_tok_per_s"])
        within = [lv for lv in mine if lv["slo_attainment"] >= min_attainment and not lv["errors"]]
        knee = max(within, key=lambda lv: lv["concurrency"]) if within else None
        out[name] = {
            "peak_out_tok_per_s": peak["out_tok_per_s"],
            "peak_at_concurrency": peak["concurrency"],
            "max_concurrency_within_slo": knee["concurrency"] if knee else None,
            "goodput_req_per_s_at_knee": knee["goodput_req_per_s"] if knee else 0,
            "single_stream_tok_per_s": next(
                (lv["decode_tok_per_s_per_req"] for lv in mine if lv["concurrency"] == 1), None
            ),
        }
    return out


CHAT_COLS = [
    ("target", "target"),
    ("concurrency", "conc"),
    ("ok", "ok"),
    ("errors", "err"),
    ("req_per_s", "req/s"),
    ("out_tok_per_s", "out tok/s"),
    ("ttft_ms_p50", "TTFT p50"),
    ("ttft_ms_p95", "TTFT p95"),
    ("tpot_ms_p50", "TPOT p50"),
    ("itl_ms_p95", "ITL p95"),
    ("e2e_s_p95", "E2E p95 s"),
    ("goodput_req_per_s", "goodput/s"),
    ("slo_attainment", "in SLO"),
]


def calibrate(client: httpx.Client, target: dict[str, Any], extra: dict[str, Any], nominal: int = 1000) -> float:
    """Words per token for the corpus on this model's tokenizer, measured by
    sending one prompt and reading the server's own prompt-token count."""
    words = sum(len(m["content"].split()) for m in build_chat_prompt(nominal, True, random.Random(1)))
    used = stream_once(client, target, build_chat_prompt(nominal, True, random.Random(1)), 1, extra)["in_tokens"]
    return min(1.5, max(0.2, words / used)) if used else 0.75


def run_chat(args: Any) -> int:
    targets = [parse_target(t) for t in args.target]
    extra = json.loads(args.extra) if args.extra else {}
    levels: list[dict[str, Any]] = []
    for t in targets:
        print(f"\n== {t['name']}  ({t['url']}, model {t['model'] or 'default'})")
        wpt = 0.75
        try:  # calibrate prompt size, then warm up (CUDA graphs, first allocations)
            with httpx.Client(timeout=600) as c:
                wpt = calibrate(c, t, extra)
                print(f"   tokenizer: {wpt:.2f} words per token on this corpus")
                for _ in range(args.warmup):
                    stream_once(c, t, build_chat_prompt(args.prompt_tokens, True, random.Random(1), wpt), 16, extra)
        except Exception as e:
            print(f"   calibration or warm-up failed: {e}")
        for c in ints(args.concurrency):
            n = max(args.requests, c * args.rounds)
            lv = chat_level(
                t,
                c,
                n,
                args.prompt_tokens,
                args.max_tokens,
                args.shared_prefix,
                args.slo_ttft_ms,
                args.slo_tpot_ms,
                extra,
                words_per_token=wpt,
            )
            levels.append(lv)
            print(table([lv], CHAT_COLS).splitlines()[-1] if len(levels) > 1 else table([lv], CHAT_COLS))
    summary = summarise_chat(levels)
    print(
        f"\nSLO: TTFT <= {args.slo_ttft_ms:g} ms and TPOT <= {args.slo_tpot_ms:g} ms "
        f"(about {1000 / args.slo_tpot_ms:.0f} tokens/s per reader)"
    )
    print(
        table(
            [{"target": k, **v} for k, v in summary.items()],
            [
                ("target", "target"),
                ("single_stream_tok_per_s", "1-stream tok/s"),
                ("peak_out_tok_per_s", "peak tok/s"),
                ("peak_at_concurrency", "at conc"),
                ("max_concurrency_within_slo", "max conc in SLO"),
                ("goodput_req_per_s_at_knee", "goodput/s"),
            ],
        )
    )
    _write(
        args.json,
        {
            "mode": "chat",
            "settings": {k: v for k, v in vars(args).items() if k not in ("func", "cmd")},
            "levels": levels,
            "summary": summary,
        },
    )
    return 0 if all(lv["errors"] == 0 for lv in levels) else 1


# ---------------------------------------------------------------- embeddings


def run_embed(args: Any) -> int:
    rows: list[dict[str, Any]] = []
    rng = random.Random(3)
    for t in [parse_target(x) for x in args.target]:
        client = httpx.Client(timeout=600, limits=httpx.Limits(max_connections=args.concurrency + 4))
        for b in ints(args.batch):
            texts = [text_of_tokens(args.text_tokens, rng) for _ in range(max(b, args.texts))]
            batches = [texts[i : i + b] for i in range(0, len(texts), b)]
            lat: list[float] = []
            tokens = 0
            dim = 0
            errors = 0

            def one(
                batch: list[str], lat: list[float] = lat, t: dict[str, Any] = t, client: httpx.Client = client
            ) -> None:
                nonlocal tokens, dim, errors
                t0 = time.perf_counter()
                try:
                    r = client.post(f"{t['url']}/embeddings", json={"model": t["model"], "input": batch})
                    r.raise_for_status()
                    d = r.json()
                    lat.append(time.perf_counter() - t0)
                    dim = len(d["data"][0]["embedding"])
                    tokens += int(
                        (d.get("usage") or {}).get("prompt_tokens") or sum(len(x.split()) / 0.75 for x in batch)
                    )
                except Exception:
                    errors += 1

            one(batches[0])  # warm-up, not counted
            lat.clear()
            tokens = 0
            t0 = time.perf_counter()
            with ThreadPoolExecutor(args.concurrency) as ex:
                list(ex.map(one, batches))
            wall = time.perf_counter() - t0
            done = len(lat) * b
            rows.append(
                {
                    "target": t["name"],
                    "batch": b,
                    "dim": dim,
                    "texts": done,
                    "errors": errors,
                    "texts_per_s": round(done / wall, 1) if wall else 0,
                    "tokens_per_s": round(tokens / wall) if wall else 0,
                    "batch_ms_p50": round(pct([x * 1000 for x in lat], 50), 1),
                    "batch_ms_p95": round(pct([x * 1000 for x in lat], 95), 1),
                    "index_1m_chunks_h": round(1e6 / (done / wall) / 3600, 1) if done and wall else None,
                }
            )
            print(table(rows[-1:], EMBED_COLS) if len(rows) == 1 else table(rows[-1:], EMBED_COLS).splitlines()[-1])
        client.close()
    _write(args.json, {"mode": "embed", "rows": rows})
    return 0 if all(r["errors"] == 0 for r in rows) else 1


EMBED_COLS = [
    ("target", "target"),
    ("batch", "batch"),
    ("dim", "dim"),
    ("errors", "err"),
    ("texts_per_s", "texts/s"),
    ("tokens_per_s", "tokens/s"),
    ("batch_ms_p50", "p50 ms"),
    ("batch_ms_p95", "p95 ms"),
    ("index_1m_chunks_h", "1M chunks (h)"),
]


# ---------------------------------------------------------------- rerank


def run_rerank(args: Any) -> int:
    rows: list[dict[str, Any]] = []
    rng = random.Random(5)
    for t in [parse_target(x) for x in args.target]:
        client = httpx.Client(timeout=600)
        for n in ints(args.candidates):
            lat: list[float] = []
            errors = 0
            for i in range(args.queries + 1):
                texts = [text_of_tokens(args.text_tokens, rng) for _ in range(n)]
                t0 = time.perf_counter()
                try:
                    r = client.post(
                        f"{t['url']}/rerank", json={"query": QUESTION, "texts": texts, "model": t["model"] or None}
                    )
                    r.raise_for_status()
                    if i:  # the first call is a warm-up
                        lat.append(time.perf_counter() - t0)
                except Exception:
                    errors += 1
            total = sum(lat)
            rows.append(
                {
                    "target": t["name"],
                    "candidates": n,
                    "errors": errors,
                    "pairs_per_s": round(len(lat) * n / total, 1) if total else 0,
                    "query_ms_p50": round(pct([x * 1000 for x in lat], 50), 1),
                    "query_ms_p95": round(pct([x * 1000 for x in lat], 95), 1),
                }
            )
            print(table(rows[-1:], RERANK_COLS) if len(rows) == 1 else table(rows[-1:], RERANK_COLS).splitlines()[-1])
        client.close()
    _write(args.json, {"mode": "rerank", "rows": rows})
    return 0 if all(r["errors"] == 0 for r in rows) else 1


RERANK_COLS = [
    ("target", "target"),
    ("candidates", "candidates"),
    ("errors", "err"),
    ("pairs_per_s", "pairs/s"),
    ("query_ms_p50", "p50 ms"),
    ("query_ms_p95", "p95 ms"),
]


# ---------------------------------------------------------------- retrieval quality


def parse_embed(spec: str) -> dict[str, Any]:
    """``hash`` (built-in, no server) or ``URL,MODEL[,QUERY_PREFIX[,DOC_PREFIX]]``."""
    if spec.strip().lower() == "hash":
        return {"name": "hash (built-in)", "env": {"SMRITI_EMBED_URL": ""}}
    parts = spec.split(",")
    url, model = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    # None removes the variable, so Smriti's per-model default applies (the
    # Qwen3-Embedding query instruction) and no prefix leaks in from the shell.
    env: dict[str, str | None] = {"SMRITI_EMBED_URL": url, "SMRITI_EMBED_MODEL": model}
    env["SMRITI_EMBED_QUERY_PREFIX"] = parts[2] if len(parts) > 2 else None
    env["SMRITI_EMBED_DOC_PREFIX"] = parts[3] if len(parts) > 3 else None
    return {"name": model or url, "env": env}


def parse_rerank(spec: str) -> dict[str, Any]:
    if spec.strip().lower() in ("none", "coverage", ""):
        return {"name": "coverage (built-in)", "env": {"SMRITI_RERANK_URL": ""}}
    parts = spec.split(",")
    env = {"SMRITI_RERANK_URL": parts[0].strip(), "SMRITI_RERANK_MODEL": parts[1].strip() if len(parts) > 1 else ""}
    return {"name": env["SMRITI_RERANK_MODEL"] or env["SMRITI_RERANK_URL"], "env": env}


def retrieval_combo(embed: dict[str, Any], rerank: dict[str, Any], pack: str | None = None) -> dict[str, Any]:
    """Index the tenant pack from scratch with one embedder and reranker, then
    score the golden set. The LLM is switched off so the score is retrieval
    only (the extractive writer answers)."""
    from .api import Platform
    from .cli import seed
    from .config import Settings
    from .evals import run_golden

    env = {**embed["env"], **rerank["env"], "SMRITI_CHAT_URL": ""}
    if pack:
        env["SMRITI_PACKS"] = pack
    saved = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        # ignore_cleanup_errors: on Windows SQLite keeps the file open until GC
        with tempfile.TemporaryDirectory(prefix="smriti-rb-", ignore_cleanup_errors=True) as tmp:
            settings = Settings.from_env(
                data_dir=Path(tmp),
                database_url=f"sqlite:///{(Path(tmp) / 'smriti.db').as_posix()}",
                scheduler=False,
                auth_mode="demo",
            )
            plat = Platform(settings)
            t0 = time.perf_counter()
            seed(plat)
            index_s = time.perf_counter() - t0
            t = plat.tenant(None)
            t1 = time.perf_counter()
            g = run_golden(t)
            per_q = (time.perf_counter() - t1) / max(1, g["cases"])
            return {
                "embedder": embed["name"],
                "reranker": rerank["name"],
                "cases": g["cases"],
                "passed": g["passed"],
                "recall_at_10": g["recall_at_10"],
                "mrr": g["mrr"],
                "citation_precision": g["citation_precision"],
                "abstention_accuracy": g["abstention_accuracy"],
                "leaks": g["leaks"],
                "index_s": round(index_s, 1),
                "query_ms": round(per_q * 1000),
                "failed": [r["id"] for r in g["rows"] if not r["pass"]],
            }
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


RETRIEVAL_COLS = [
    ("embedder", "embedder"),
    ("reranker", "reranker"),
    ("passed", "passed"),
    ("recall_at_10", "recall@10"),
    ("mrr", "MRR"),
    ("citation_precision", "cite prec"),
    ("abstention_accuracy", "abstain ok"),
    ("leaks", "leaks"),
    ("index_s", "index s"),
    ("query_ms", "ms/query"),
]


def run_retrieval(args: Any) -> int:
    rows = []
    for e in [parse_embed(x) for x in args.embed or ["hash"]]:
        for r in [parse_rerank(x) for x in args.rerank or ["none"]]:
            print(f"\n== {e['name']} + {r['name']}")
            try:
                row = retrieval_combo(e, r, args.pack)
            except Exception as ex:
                row = {"embedder": e["name"], "reranker": r["name"], "error": f"{type(ex).__name__}: {ex}"[:300]}
                print(f"   failed: {row['error']}")
            rows.append(row)
    print()
    print(table(rows, RETRIEVAL_COLS))
    _write(args.json, {"mode": "retrieval", "rows": rows})
    return 0 if all("error" not in r and r["leaks"] == 0 for r in rows) else 1


# ---------------------------------------------------------------- CLI


def add_parser(sub: Any) -> None:
    mp = sub.add_parser(
        "modelbench", help="benchmark model servers: compare models, sweep concurrency, tune throughput"
    )
    ms = mp.add_subparsers(dest="mode", required=True)

    c = ms.add_parser("chat", help="LLM latency and throughput across concurrency levels")
    c.add_argument("--target", action="append", required=True, help="URL,MODEL[,NAME] of an OpenAI-compatible server")
    c.add_argument("--concurrency", default="1,2,4,8,16")
    c.add_argument("--requests", type=int, default=16, help="requests per level (at least concurrency x rounds)")
    c.add_argument("--rounds", type=int, default=3, help="minimum requests per concurrent stream")
    c.add_argument("--prompt-tokens", type=int, default=1500, help="about this many prompt tokens (a RAG prompt)")
    c.add_argument("--max-tokens", type=int, default=256)
    c.add_argument("--shared-prefix", action="store_true", help="same passages every time (prefix-cache best case)")
    c.add_argument("--slo-ttft-ms", type=float, default=2000)
    c.add_argument("--slo-tpot-ms", type=float, default=100, help="100 ms per token is about 10 tokens/s per reader")
    c.add_argument(
        "--extra", help='JSON merged into every request, e.g. \'{"chat_template_kwargs":{"enable_thinking":false}}\''
    )
    c.add_argument("--warmup", type=int, default=2)
    c.add_argument("--json")
    c.set_defaults(func=run_chat)

    e = ms.add_parser("embed", help="embedding throughput by batch size")
    e.add_argument("--target", action="append", required=True, help="URL,MODEL[,NAME] (base URL ending in /v1)")
    e.add_argument("--batch", default="1,8,32")
    e.add_argument("--texts", type=int, default=128)
    e.add_argument("--text-tokens", type=int, default=350, help="tokens per text (about one chunk)")
    e.add_argument("--concurrency", type=int, default=2)
    e.add_argument("--json")
    e.set_defaults(func=run_embed)

    r = ms.add_parser("rerank", help="cross-encoder latency by number of candidates")
    r.add_argument("--target", action="append", required=True, help="URL,MODEL[,NAME] of a TEI reranker")
    r.add_argument("--candidates", default="10,30,50")
    r.add_argument("--queries", type=int, default=10)
    r.add_argument("--text-tokens", type=int, default=350)
    r.add_argument("--json")
    r.set_defaults(func=run_rerank)

    q = ms.add_parser("retrieval", help="golden-set quality for every embedder x reranker combination")
    q.add_argument("--embed", action="append", help='"hash" or URL,MODEL[,QUERY_PREFIX[,DOC_PREFIX]]')
    q.add_argument("--rerank", action="append", help='"none" or URL,MODEL')
    q.add_argument("--pack", help="tenant pack to index (default: SMRITI_PACKS or the demo pack)")
    q.add_argument("--json")
    q.set_defaults(func=run_retrieval)
