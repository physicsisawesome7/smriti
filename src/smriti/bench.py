"""Load test for a running Smriti server.

    smriti bench --url http://127.0.0.1:8000 --concurrency 8 --requests 200
    smriti bench --no-cache          # every question unique: measures the GPU path

Reports p50/p95/p99 latency, answers per second, how many answers needed the
LLM, prompt and completion tokens per answer (from the server's own meters),
cache hit rate and load-shed count. Run it before and after any tuning change;
the price list for a deployment should be built on these numbers.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

QUESTIONS = [
    ("asha", "What is the period for submitting the performance guarantee under clause 16.1?"),
    ("asha", "What is the maximum liquidated damages under clause 46.1?"),
    (
        "meera",
        "Up to what value can the Chief Engineer (Construction) accept a works tender, and above what value is finance vetting needed?",
    ),
    ("asha", "What is the estimated value and completion period of tender NR-DLI-ENG-2025-14?"),
    ("asha", "पटरी रखरखाव के दौरान सुरक्षा के क्या निर्देश हैं?"),
    ("rakesh", "What is the total area in hectare of plots in village Rampur?"),
    ("asha", "Summarise the monsoon preparedness circular."),
    ("vikram", "What did the tender committee recommend for the doubling works and why?"),
    ("meera", "When is Associate Finance vetting needed before issuing a letter of acceptance?"),
    ("asha", "What happens if the contractor submits the performance guarantee late?"),
    ("bidder", "How much bid security must a bidder furnish?"),
    ("asha", "What is the canteen subsidy for night shift staff?"),
]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


METERS = ("llm_calls", "prompt_tokens", "completion_tokens", "shed")


def run_bench(
    url: str,
    concurrency: int,
    requests: int,
    unique: bool = False,
    metrics_token: str | None = None,
    metrics_user: str | None = None,
    json_path: str | None = None,
) -> int:
    client = httpx.Client(base_url=url.rstrip("/"), timeout=180, limits=httpx.Limits(max_connections=concurrency * 2))
    token = metrics_token or os.environ.get("SMRITI_METRICS_TOKEN", "")
    auth = {"Authorization": f"Bearer {token}"} if token else {"X-Smriti-User": metrics_user} if metrics_user else {}

    def metrics() -> dict[str, Any] | None:
        """The server's token meters, or None when this caller may not read them."""
        try:
            r = client.get("/api/metrics.json", headers=auth)
            d = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        return d if isinstance(d, dict) and all(k in d for k in METERS) else None

    before = metrics()
    if before is None:
        print(
            "note: no access to /api/metrics.json, so token counts are left out. Set SMRITI_METRICS_TOKEN "
            "(or --metrics-token), or pass --metrics-user with someone who has the audit grant.",
            flush=True,
        )
    lat: list[float] = []
    rows: list[dict[str, Any]] = []
    errors = 0

    def one(i: int) -> None:
        nonlocal errors
        user, q = QUESTIONS[i % len(QUESTIONS)]
        if unique:  # an identifier makes the question distinct to the answer cache
            q = f"{q} (ref BENCH-{random.randint(100000, 999999)})"
        t0 = time.perf_counter()
        try:
            r = client.post("/api/ask", json={"question": q}, headers={"X-Smriti-User": user})
            r.raise_for_status()
            d = r.json()
            lat.append(time.perf_counter() - t0)
            rows.append({"model": d.get("model"), "abstained": d.get("abstained"), "cached": d.get("cached", False)})
        except httpx.HTTPError:
            errors += 1

    t0 = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as ex:
        list(ex.map(one, range(requests)))
    wall = time.perf_counter() - t0
    after = metrics() if before is not None else None

    n = len(rows)
    report: dict[str, Any] = {
        "requests": requests,
        "concurrency": concurrency,
        "ok": n,
        "errors": errors,
        "wall_s": round(wall, 2),
        "answers_per_s": round(n / wall, 2) if wall else 0,
        "latency_ms": {
            "p50": round(_pct(lat, 50) * 1000),
            "p95": round(_pct(lat, 95) * 1000),
            "p99": round(_pct(lat, 99) * 1000),
            "mean": round(statistics.mean(lat) * 1000) if lat else 0,
        },
        "cached": sum(r["cached"] for r in rows),
        "abstained": sum(bool(r["abstained"]) for r in rows),
        "answered_by": {m: sum(1 for r in rows if r["model"] == m) for m in sorted({str(r["model"]) for r in rows})},
    }
    if before is not None and after is not None:
        llm_calls = after["llm_calls"] - before["llm_calls"]
        prompt = after["prompt_tokens"] - before["prompt_tokens"]
        completion = after["completion_tokens"] - before["completion_tokens"]
        report.update(
            {
                "llm_calls": llm_calls,
                "llm_share": round(llm_calls / n, 2) if n else 0,
                "prompt_tokens_per_llm_call": round(prompt / llm_calls) if llm_calls else 0,
                "completion_tokens_per_llm_call": round(completion / llm_calls) if llm_calls else 0,
                "gpu_tokens_per_answer": round((prompt + completion) / n, 1) if n else 0,
                "load_shed": after["shed"] - before["shed"],
            }
        )
    if json_path:
        Path(json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    width = max(len(k) for k in report)
    for k, v in report.items():
        print(f"{k:<{width}}  {v}")
    return 0 if errors == 0 else 1
