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
import json
import os
import sys
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="smriti", description="Smriti by Safearly: institutional memory")
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
    sub.add_parser("worker", help="run source schedules and background jobs (one per deployment)")
    bp = sub.add_parser("bench", help="load-test a running server and report latency, throughput and GPU tokens")
    bp.add_argument("--url", default="http://127.0.0.1:8000")
    bp.add_argument("--concurrency", type=int, default=8)
    bp.add_argument("--requests", type=int, default=100)
    bp.add_argument(
        "--no-cache", action="store_true", help="make every question unique so the answer cache cannot help"
    )
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
    ep.add_argument("kind", nargs="?", default="gate", choices=["golden", "leakage", "redteam", "gate"])
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

    if args.cmd == "worker":
        from .connectors import JobRunner
        from .watch import Scheduler

        plat = _platform(args)
        n = sum(len(t.pack.sources) for t in plat.tenants.values())
        print(f"worker: {n} pack sources on schedule; running ingestion jobs", flush=True)
        JobRunner(plat.tenants, plat.box).start()
        sched = Scheduler(plat.tenants, plat.box)
        sched.run()  # blocks
        return 0

    if args.cmd == "sandbox":
        from .agents.sandbox import serve

        serve(args.host, args.port)
        return 0

    if args.cmd == "bench":
        from .bench import run_bench

        return run_bench(args.url, args.concurrency, args.requests, unique=args.no_cache)

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
