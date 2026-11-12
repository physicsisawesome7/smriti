"""``smriti`` command line.

smriti serve                 run the API and web app
smriti seed                  ingest the tenant pack's seed documents
smriti ingest FILE...        ingest files as a user
smriti crawl SOURCE          run one source adapter now
smriti ask "question" --as USER [--as-of DATE]
smriti eval [golden|leakage|redteam|gate]   release gates (exit 1 on failure)
smriti audit-verify          check the hash chain
smriti validate-pack DIR     validate a tenant pack without starting anything
smriti mcp --as USER         MCP server on stdio
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from .config import Settings, load_pack

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _platform(args: argparse.Namespace) -> Any:
    from .api import Platform

    s = Settings.from_env(scheduler=False)
    return Platform(s)


def _utf8_console() -> None:
    # Windows consoles default to a legacy code page; Hindi titles must not crash a seed run
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def seed(plat: Any, tenant_id: str | None = None) -> list[dict[str, Any]]:
    _utf8_console()
    t = plat.tenant(tenant_id)
    out = []
    for item in t.pack.config.seed:
        f = t.pack.resolve(item["path"])
        if not f:
            print(f"  missing seed file {item['path']}", file=sys.stderr)
            continue
        files = (
            sorted(p for p in f.rglob("*") if p.is_file() and not p.name.endswith(".meta.yaml")) if f.is_dir() else [f]
        )
        for p in files:
            hints = {k: v for k, v in item.items() if k not in ("path", "source")}
            side = p.with_name(p.name + ".meta.yaml")
            if side.exists():
                hints.update(yaml.safe_load(side.read_text(encoding="utf-8")) or {})
            res = t.ingest(
                p.read_bytes(), p.name, actor="seed", source=item.get("source", "seed"), hints=hints, trust="pack"
            )
            out.append(res)
            print(f"  {res['status']:<12} {res.get('doc_id', '')}  {res.get('title', p.name)}")
    return out


HEARTBEAT_EVERY_S = 15


def heartbeat_path() -> Path:
    return Path(os.environ.get("SMRITI_HEARTBEAT_FILE") or Path(tempfile.gettempdir()) / "smriti-worker.heartbeat")


def _worker_heartbeat(threads: list[threading.Thread]) -> threading.Thread:
    """Touch the heartbeat file while every worker thread is alive. The
    container health check (``smriti worker --healthcheck``) reads its age, so
    a worker whose job runner died stops looking healthy."""
    path = heartbeat_path()

    def beat() -> None:
        while all(th.is_alive() for th in threads):
            with contextlib.suppress(OSError):
                path.write_text(str(int(time.time())), encoding="utf-8")
            time.sleep(HEARTBEAT_EVERY_S)

    th = threading.Thread(target=beat, daemon=True, name="smriti-heartbeat")
    th.start()
    return th


def worker_healthy(max_age_s: float | None = None) -> bool:
    limit = 4 * HEARTBEAT_EVERY_S if max_age_s is None else max_age_s
    try:
        return time.time() - heartbeat_path().stat().st_mtime <= limit
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="smriti", description="Smriti by Safearly: institutional memory")
    from . import __version__

    ap.add_argument("--version", action="version", version=f"smriti {__version__}")
    ap.add_argument("--data-dir", help="where the database, blobs and caches live (default ./data or $SMRITI_DATA_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--seed", action="store_true", help="seed an empty tenant on start")
    sp.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("SMRITI_WORKERS", "1")),
        help="API processes (needs the qdrant backend when >1)",
    )
    sub.add_parser("seed")
    wp = sub.add_parser("worker", help="run source schedules and background jobs (one per deployment)")
    wp.add_argument(
        "--healthcheck", action="store_true", help="exit 0 if a running worker wrote its heartbeat recently"
    )
    bp = sub.add_parser("bench", help="load-test a running server and report latency, throughput and GPU tokens")
    bp.add_argument("--url", default="http://127.0.0.1:8000")
    bp.add_argument("--concurrency", type=int, default=8)
    bp.add_argument("--requests", type=int, default=100)
    bp.add_argument("--metrics-token", help="token for the server's meters (default: $SMRITI_METRICS_TOKEN)")
    bp.add_argument("--metrics-user", help="demo mode: read the meters as this person (needs the audit grant)")
    bp.add_argument("--json", help="also write the report to this file")
    bp.add_argument(
        "--no-cache", action="store_true", help="make every question unique so the answer cache cannot help"
    )
    from .modelbench import add_parser as add_modelbench

    add_modelbench(sub)
    ip = sub.add_parser("ingest")
    ip.add_argument("files", nargs="+")
    ip.add_argument("--as", dest="user", default="km")
    cp = sub.add_parser("crawl")
    cp.add_argument("source")
    qp = sub.add_parser("ask")
    qp.add_argument("question")
    qp.add_argument("--as", dest="user", required=True)
    qp.add_argument("--as-of", dest="as_of")
    ep = sub.add_parser("eval")
    ep.add_argument(
        "kind", nargs="?", default="gate", choices=["golden", "leakage", "redteam", "gate", "hard", "compare", "build"]
    )
    ep.add_argument("configs", nargs="*", help="hard/compare: NAME or NAME:KEY=VALUE,KEY=VALUE (environment settings)")
    ep.add_argument("--out", help="hard: keep the generated pack here; build: where to write the candidates")
    ep.add_argument("--pack", help="hard/compare: score this tenant pack's golden set instead of the hard set")
    ep.add_argument("--seed", type=int, default=7, help="hard/compare: seed of the generated corpus")
    ep.add_argument("--json", help="hard/compare: also write the full result to this file")
    ep.add_argument("--count", type=int, default=40, help="build: how many candidate questions to draft")
    ep.add_argument("--as", dest="user", help="build: draft as this person (they must be able to read the documents)")
    sub.add_parser("audit-verify")
    vp = sub.add_parser("validate-pack")
    vp.add_argument("path")
    xp = sub.add_parser("sandbox", help="run the isolated code-execution service for agents")
    xp.add_argument("--host", default="0.0.0.0")  # noqa: S104 - its own internal-only network
    xp.add_argument("--port", type=int, default=8080)
    mp = sub.add_parser("mcp")
    mp.add_argument("--as", dest="user", required=True)
    args = ap.parse_args(argv)
    _utf8_console()
    if args.data_dir:
        os.environ["SMRITI_DATA_DIR"] = args.data_dir

    if args.cmd == "validate-pack":
        pack = load_pack(Path(args.path))
        print(
            json.dumps(
                {
                    "tenant": pack.id,
                    "layers": [str(r) for r in pack.roots],
                    "roles": len(pack.roles.roles),
                    "users": len(pack.roles.users),
                    "sources": [s.source for s in pack.sources],
                    "skills": list(pack.skills),
                    "golden": len(pack.evals["golden"]),
                    "redteam": len(pack.evals["redteam"]),
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        # Smriti's own address, used by demo presets and pack sources that point at it
        me = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host  # noqa: S104 - comparing, not binding
        os.environ.setdefault("SMRITI_SELF_URL", f"http://{me}:{args.port}")
        os.environ.setdefault("SMRITI_PUBLIC_URL", os.environ["SMRITI_SELF_URL"])
        s = Settings.from_env()
        exposed = os.environ.get("SMRITI_PUBLISH_BIND") or args.host  # Docker: the host side of the port mapping
        if s.auth_mode == "demo" and exposed not in LOOPBACK and os.environ.get("SMRITI_DEMO_INSECURE") != "1":
            print(
                f"refusing to serve demo auth on {exposed}: anyone who can reach it can claim to be any user.\n"
                "Use SMRITI_AUTH=token with SMRITI_API_TOKENS (or an SSO proxy), bind to 127.0.0.1, "
                "or set SMRITI_DEMO_INSECURE=1 for a throwaway demo.",
                file=sys.stderr,
            )
            return 2
        workers = args.workers
        if workers > 1 and s.vector_backend != "qdrant":
            print("--workers > 1 needs SMRITI_VECTOR_BACKEND=qdrant (each process would hold its own index); using 1")
            workers = 1
        if args.seed:
            plat = _platform(args)
            if not plat.store.list_documents(plat.default):
                print("Seeding demo tenant…")
                seed(plat)
            del plat
        if workers > 1:
            uvicorn.run(
                "smriti.api:create_app", factory=True, host=args.host, port=args.port, workers=workers, log_level="info"
            )
        else:
            uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
        return 0

    if args.cmd == "worker" and args.healthcheck:
        return 0 if worker_healthy() else 1

    if args.cmd == "worker":
        from .connectors import JobRunner
        from .watch import Scheduler

        plat = _platform(args)
        n = sum(len(t.pack.sources) for t in plat.tenants.values())
        print(f"worker: {n} pack sources on schedule; running ingestion jobs", flush=True)
        jobs = JobRunner(plat.tenants, plat.box)
        jobs.start()
        sched = Scheduler(plat.tenants, plat.box)
        _worker_heartbeat([jobs, threading.main_thread()])
        sched.run()  # blocks
        return 0

    if args.cmd == "sandbox":
        from .agents.sandbox import serve

        serve(args.host, args.port)
        return 0

    if args.cmd == "bench":
        from .bench import run_bench

        return run_bench(
            args.url,
            args.concurrency,
            args.requests,
            unique=args.no_cache,
            metrics_token=args.metrics_token,
            metrics_user=args.metrics_user,
            json_path=args.json,
        )

    if args.cmd == "modelbench":
        return int(args.func(args))

    if args.cmd == "mcp":
        from .mcp_server import serve_stdio

        serve_stdio(_platform(args), args.user)
        return 0

    plat = _platform(args)
    t = plat.tenant(None)
    if args.cmd == "seed":
        seed(plat)
    elif args.cmd == "ingest":
        for f in args.files:
            p = Path(f)
            print(json.dumps(t.ingest(p.read_bytes(), p.name, actor=args.user, trust="curator"), indent=2, default=str))
    elif args.cmd == "crawl":
        from .watch import run_source

        print(json.dumps(run_source(t, args.source, actor="cli"), indent=2, default=str))
    elif args.cmd == "ask":
        from .ask import ask

        r = ask(t, t.principal(args.user), args.question, as_of=args.as_of)
        print(r["answer"])
        for c in r["citations"]:
            print(
                f"  [{c['n']}] {c['title']} {c['ref_no'] or ''} {('clause ' + c['clause_id']) if c['clause_id'] else ''} ({c['status']})"
            )
        print(f"  faithfulness={r['verification'].get('faithfulness')} abstained={r['abstained']} route={r['route']}")
    elif args.cmd == "eval" and args.kind in ("hard", "compare"):
        from . import evals

        specs = args.configs or ["default"]
        if args.kind == "compare" and len(specs) != 2:
            print("compare needs two configurations, for example: smriti eval compare base new:SMRITI_RRF_K=20")
            return 2
        runs = [evals.run_hard(evals.parse_config(s), out=args.out, seed=args.seed, pack=args.pack) for s in specs]
        for r in runs:
            print(evals.format_hard(r), end=chr(10) * 2)
        result: dict[str, Any] = runs[0] if len(runs) == 1 else {"runs": runs}
        if args.kind == "compare":
            result["compare"] = evals.compare(runs[0], runs[1])
            print(evals.format_compare(result["compare"]))
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        fails = [f"{r['config']}: {why}" for r in runs for why in evals.hard_gate(r)]
        for why in fails:
            print(f"FAIL {why}")
        return 1 if fails else 0
    elif args.cmd == "eval" and args.kind == "build":
        from . import evals

        if not args.user:
            print("build needs --as USER: candidates are drafted only from documents that person may read")
            return 2
        t = _platform(args).tenant(None)
        built = evals.build_candidates(t, args.user, count=args.count, seed=args.seed)
        out = Path(args.out or "golden-candidates.yaml")
        head = (
            "# Candidate golden questions drafted by `smriti eval build`. Review every one: fix the wording,\n"
            "# delete what is wrong or trivial, then move the cases you accept into the pack's evals/golden.yaml.\n"
        )
        out.write_text(
            head + yaml.safe_dump({"cases": built["cases"]}, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        n = len(built["cases"])
        print(
            f"{n} candidates written to {out} (drafted by {built['drafted_by']}; "
            f"{built['rejected_drafts']} drafts rejected; Smriti answers {built['passes_now']} of {n} correctly today)"
        )
        return 0
    elif args.cmd == "eval":
        from . import evals

        fn = {
            "golden": evals.run_golden,
            "leakage": evals.leakage_suite,
            "redteam": evals.redteam,
            "gate": evals.release_gate,
        }[args.kind]
        res = fn(t)
        print(json.dumps(res, indent=2, default=str))
        ok = res.get("pass", res.get("ok", True)) if args.kind in ("gate", "leakage") else True
        return 0 if ok else 1
    elif args.cmd == "audit-verify":
        v = t.audit.verify()
        print(json.dumps(v))
        return 0 if v["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
