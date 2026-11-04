"""Ingestion connectors and the job runner.

    registry           every connector kind and the form fields it needs
    preview()          dry run: discover and parse a few items, ingest nothing
    create/update()    connector configs, with secrets sealed at rest
    enqueue()          a job for a connector run or an upload batch
    JobRunner          background thread that executes queued jobs

Jobs run in the worker container in Docker (SMRITI_SCHEDULER=1 there) or in
the API process for single-box installs. Each job records live progress,
per-item outcomes and a log; it can be cancelled between items; one failing
item never fails the job.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterable
from typing import Any

from ..store import connectors as connectors_t
from ..store import ingest_jobs
from .base import Connector, ConnectorError, Ctx, Item
from .files import FolderConnector, UploadConnector
from .mcp_source import McpConnector
from .rest import ApiConnector
from .sql import DatabaseConnector
from .web import FeedConnector, UrlConnector, WebsiteConnector

REGISTRY: dict[str, type[Connector]] = {
    c.kind: c
    for c in (
        WebsiteConnector,
        UrlConnector,
        UploadConnector,
        ApiConnector,
        DatabaseConnector,
        McpConnector,
        FeedConnector,
        FolderConnector,
    )
}
ACCESS_KEYS = ("doc_class", "divisions", "projects", "groups")
MAX_ITEM_LOG = 400
MAX_LOG_LINES = 300

__all__ = ["REGISTRY", "Connector", "ConnectorError", "Ctx", "Item", "JobRunner"]


# ---------------------------------------------------------------- registry and configs


def types() -> list[dict[str, Any]]:
    return [
        {"kind": c.kind, "label": c.label, "description": c.description, "icon": c.icon, "fields": c.fields}
        for c in REGISTRY.values()
    ]


def split_config(kind: str, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cls = REGISTRY.get(kind)
    if not cls:
        raise ConnectorError(f"unknown connector kind '{kind}'")
    secret_names = {f["name"] for f in cls.fields if f["type"] == "secret"}
    known = {f["name"] for f in cls.fields}
    config = {k: v for k, v in values.items() if k in known and k not in secret_names}
    secrets = {k: v for k, v in values.items() if k in secret_names and v not in (None, "")}
    return config, secrets


def view(row: dict[str, Any], box: Any) -> dict[str, Any]:
    """A connector as the API returns it: secret values never leave the server."""
    sealed = box.open(row["secrets"]) if row["secrets"] else {}
    cls = REGISTRY.get(row["kind"])
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "label": cls.label if cls else row["kind"],
        "icon": cls.icon if cls else "◇",
        "config": json.loads(row["config"] or "{}"),
        "secrets_set": sorted(sealed),
        "schedule": row["schedule"],
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
        "state": {k: v for k, v in json.loads(row["state"] or "{}").items() if k in ("cursor", "last_counts")},
    }


def save(
    t: Any, box: Any, actor: str, name: str, kind: str, values: dict[str, Any], connector_id: int | None = None
) -> int:
    config, secrets = split_config(kind, values)
    now = time.time()
    schedule = (config.get("schedule") or "").strip() or None
    if connector_id is None:
        cid = t.store.insert(
            connectors_t,
            tenant=t.id,
            name=name,
            kind=kind,
            config=json.dumps(config),
            secrets=box.seal(secrets),
            access="{}",
            doc_type=config.get("doc_type"),
            schedule=schedule,
            enabled=True,
            state="{}",
            created_by=actor,
            created_at=now,
            updated_at=now,
        )
        t.audit.record(actor, "connector_create", connector_id=cid, kind=kind, name=name, secrets_set=sorted(secrets))
        return cid
    row = get_row(t, connector_id)
    merged = {
        **(box.open(row["secrets"]) if row["secrets"] else {}),
        **secrets,
    }  # blank secret fields keep the stored value
    t.store.update_row(
        connectors_t,
        {"id": connector_id, "tenant": t.id},
        name=name,
        config=json.dumps(config),
        secrets=box.seal(merged),
        doc_type=config.get("doc_type"),
        schedule=schedule,
        updated_at=now,
    )
    t.audit.record(actor, "connector_update", connector_id=connector_id, name=name, secrets_changed=sorted(secrets))
    return connector_id


def get_row(t: Any, cid: int) -> dict[str, Any]:
    rows = t.store.rows(connectors_t, tenant=t.id, id=cid)
    if not rows:
        raise KeyError(cid)
    return rows[0]


def access_hints(config: dict[str, Any]) -> dict[str, Any]:
    def lst(v: Any, default: list[str]) -> list[str]:
        if isinstance(v, list):
            return v
        return [x.strip() for x in str(v or "").split(",") if x.strip()] or default

    hints: dict[str, Any] = {
        "access": {
            "doc_class": config.get("doc_class") or "internal",
            "divisions": lst(config.get("divisions"), ["*"]),
            "projects": lst(config.get("projects"), []),
            "groups": lst(config.get("groups"), []),
        }
    }
    if config.get("doc_type"):
        hints["doc_type"] = config["doc_type"]
    return hints


# ---------------------------------------------------------------- preview


def preview(
    t: Any,
    kind: str,
    values: dict[str, Any],
    stored_secrets: dict[str, Any] | None = None,
    limit: int = 5,
    deadline_s: float = 45.0,
) -> dict[str, Any]:
    """Discover a few items and parse the first ones; nothing is ingested."""
    from ..parsers import parse

    config, secrets = split_config(kind, values)
    secrets = {**(stored_secrets or {}), **secrets}
    start = time.monotonic()
    logs: list[str] = []
    ctx = Ctx(
        config=config,
        secrets=secrets,
        state={},
        log=logs.append,
        cancelled=lambda: time.monotonic() - start > deadline_s,
        tenant=t,
    )
    conn = REGISTRY[kind](ctx)
    out: list[dict[str, Any]] = []
    try:
        conn.validate()
        for item in conn.discover():
            entry: dict[str, Any] = {"title": item.title, "filename": item.filename, "url": item.url, "key": item.key}
            if len(out) < 3:
                try:
                    data = item.fetch()
                    doc = parse(data, item.filename)
                    entry.update(
                        size=len(data),
                        parser=doc.parser,
                        pages=doc.pages,
                        title=item.title or doc.title,
                        snippet=doc.text[:500],
                        blocks=len(doc.blocks),
                        warnings=doc.warnings[:3],
                    )
                except Exception as e:
                    entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            out.append(entry)
            if len(out) >= limit or ctx.cancelled():
                break
    except Exception as e:
        return {"ok": False, "error": str(e)[:500], "items": out, "log": logs[-20:]}
    finally:
        conn.close()
    return {
        "ok": True,
        "items": out,
        "more": len(out) >= limit,
        "log": logs[-20:],
        "elapsed_s": round(time.monotonic() - start, 2),
    }


# ---------------------------------------------------------------- jobs


def enqueue(
    t: Any, actor: str, *, connector_id: int | None = None, kind: str, title: str, payload: dict[str, Any] | None = None
) -> int:
    if connector_id is not None:
        active = [
            j
            for j in t.store.rows(ingest_jobs, tenant=t.id, connector_id=connector_id)
            if j["status"] in ("queued", "running")
        ]
        if active:
            return int(active[0]["id"])  # one run per connector at a time
    jid = t.store.insert(
        ingest_jobs,
        tenant=t.id,
        connector_id=connector_id,
        kind=kind,
        title=title,
        payload=json.dumps(payload or {}),
        status="queued",
        stats=json.dumps({}),
        items="[]",
        log="",
        created_by=actor,
        created_at=time.time(),
    )
    t.audit.record(actor, "ingest_job_queued", job_id=jid, connector_id=connector_id, kind=kind)
    return jid


def job_view(row: dict[str, Any], with_items: bool = False) -> dict[str, Any]:
    out = {
        k: row[k]
        for k in (
            "id",
            "connector_id",
            "kind",
            "title",
            "status",
            "error",
            "created_by",
            "created_at",
            "started_at",
            "finished_at",
        )
    }
    out["stats"] = json.loads(row["stats"] or "{}")
    if with_items:
        out["items"] = json.loads(row["items"] or "[]")
        out["log"] = row["log"].splitlines()[-MAX_LOG_LINES:]
    return out


def cancel(t: Any, jid: int, actor: str) -> None:
    n = t.store.update_row(
        ingest_jobs, {"id": jid, "tenant": t.id, "status": "queued"}, status="cancelled", finished_at=time.time()
    )
    if not n:
        t.store.update_row(ingest_jobs, {"id": jid, "tenant": t.id, "status": "running"}, status="cancelling")
    t.audit.record(actor, "ingest_job_cancel", job_id=jid)


def _derived_doc_id(cid: int | None, item: Item) -> str | None:
    if item.doc_id:
        return item.doc_id
    if (item.url or "").startswith(("http://", "https://")):
        return None  # URL-based ids: the same page found by two sources is one document
    if cid is None:
        return None  # uploads: named by file, so a re-uploaded revision becomes a new version
    return f"c{cid}-" + hashlib.sha256(item.key.encode()).hexdigest()[:16]


def run_job(t: Any, box: Any, jid: int) -> dict[str, Any]:
    """Execute one claimed job to completion. Returns its final view."""
    job = t.store.rows(ingest_jobs, tenant=t.id, id=jid)[0]
    payload = json.loads(job["payload"] or "{}")
    log_lines: list[str] = []
    results: list[dict[str, Any]] = []
    stats = {"found": 0, "ingested": 0, "new_versions": 0, "unchanged": 0, "failed": 0, "removed": 0, "bytes": 0}
    last_flush = [0.0]
    cancelled_flag = [False]
    last_cancel_check = [0.0]

    def log(msg: str) -> None:
        log_lines.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def flush(force: bool = False) -> None:
        now = time.time()
        if force or now - last_flush[0] > 0.5:
            last_flush[0] = now
            t.store.update_row(
                ingest_jobs,
                {"id": jid},
                stats=json.dumps(stats),
                items=json.dumps(results[-MAX_ITEM_LOG:]),
                log="\n".join(log_lines[-MAX_LOG_LINES:]),
                heartbeat=now,
            )

    def cancelled() -> bool:
        if not cancelled_flag[0] and time.monotonic() - last_cancel_check[0] > 1.5:
            last_cancel_check[0] = time.monotonic()
            row = t.store.rows(ingest_jobs, id=jid)
            cancelled_flag[0] = bool(row and row[0]["status"] == "cancelling")
        return cancelled_flag[0]

    cid = job["connector_id"]
    conn_row = get_row(t, cid) if cid is not None else None
    if conn_row is not None:
        kind = conn_row["kind"]
        config = json.loads(conn_row["config"] or "{}")
        secrets = box.open(conn_row["secrets"]) if conn_row["secrets"] else {}
        state = json.loads(conn_row["state"] or "{}")
        source = f"connector:{conn_row['name']}"
    else:
        kind = job["kind"]
        config = payload.get("config", {})
        secrets, state, source = {}, {}, "upload"
    config = {**config, **payload.get("overrides", {})}
    if kind == "upload":
        config["files"] = payload.get("files", [])
    hints_base = access_hints(config)
    creator_curates = False
    try:
        cp = t.principal(job["created_by"])
        creator_curates = cp.can("curate") or cp.can("administer")
    except Exception:  # noqa: S110 - creator left the directory: run with content trust
        pass
    trust = "curator" if creator_curates else "content"
    apply_rel = bool(config.get("apply_relations")) and creator_curates
    ctx = Ctx(config=config, secrets=secrets, state=state, log=log, cancelled=cancelled, connector_id=cid, tenant=t)
    connector = REGISTRY[kind](ctx)
    max_items = int(config.get("max_items") or 500)
    seen_keys: dict[str, str] = {}
    complete = False
    error = None
    log(f"started {kind} job by {job['created_by']}")
    try:
        connector.validate()
        for item in connector.discover():
            if cancelled():
                log("cancelled")
                break
            if stats["found"] >= max_items:
                log(f"stopped at max_items={max_items}")
                break
            stats["found"] += 1
            entry: dict[str, Any] = {"title": item.title or item.filename, "url": item.url, "key": item.key[:200]}
            try:
                data = item.fetch()
                stats["bytes"] += len(data)
                res = t.ingest(
                    data,
                    item.filename,
                    actor=job["created_by"],
                    source=source,
                    source_url=item.url if item.url and item.url.startswith(("http://", "https://")) else None,
                    hints={**hints_base, **item.hints},
                    doc_id=_derived_doc_id(cid, item),
                    legal_status=config.get("legal_status"),
                    trust=trust,
                    apply_relations=apply_rel,
                )
                status = res["status"]
                if status == "archive":
                    members = res.get("members", [])
                    stats["ingested"] += sum(1 for m in members if m.get("status") == "ingested")
                    stats["unchanged"] += sum(1 for m in members if m.get("status") == "duplicate")
                    entry.update(status="archive", members=len(members))
                else:
                    stats[
                        "ingested"
                        if status == "ingested"
                        else "new_versions"
                        if status == "new_version"
                        else "unchanged"
                    ] += 1
                    entry.update(
                        status=status, doc_id=res.get("doc_id"), version=res.get("version"), doc_title=res.get("title")
                    )
                    if res.get("access_tightened"):
                        entry["note"] = "already in the library from another source; access tightened to satisfy both"
                        log(f"access tightened on {res.get('doc_id')} (same content from a stricter source)")
                    if res.get("doc_id"):
                        seen_keys[item.key] = res["doc_id"]
            except Exception as e:
                stats["failed"] += 1
                entry.update(status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")
                log(f"failed {item.filename}: {e}")
            results.append(entry)
            flush()
        else:
            complete = True
    except ConnectorError as e:
        error = str(e)
        log(f"error: {e}")
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:500]}"
        log(f"error: {error}")
    finally:
        connector.close()

    # Removal detection only after a full, clean discovery. A failed request, a failed item,
    # or a run that finds nothing where there was something is an outage, not a removal.
    prev: dict[str, str] = state.get("docs", {}) if conn_row is not None else {}
    clean = complete and not error and not stats["failed"] and not getattr(connector, "errors", 0)
    if conn_row is not None and clean and not (prev and not seen_keys):
        misses: dict[str, int] = {k: v for k, v in state.get("misses", {}).items() if k not in seen_keys}
        need = max(1, int(config.get("removal_misses") or 3))
        gone = []
        for k in prev:
            if k in seen_keys:
                continue
            misses[k] = misses.get(k, 0) + 1
            if misses[k] >= need:
                gone.append(k)
        stats["removed"] = len(gone)
        keep = {k: prev[k] for k in prev if k not in seen_keys and k not in gone}
        if gone and config.get("on_removed") == "tombstone":
            for k in gone:
                try:
                    t.tombstone(prev[k], job["created_by"], f"no longer present in source {conn_row['name']}")
                except Exception as e:
                    log(f"could not tombstone {prev[k]}: {e}")
        elif gone:
            log(f"{len(gone)} items no longer in the source (kept; set 'tombstone' to retire them)")
        if keep:
            log(f"{len(keep)} items missing this run; counted, not yet removed (needs {need} runs in a row)")
        state["docs"] = {**seen_keys, **keep}
        state["misses"] = {k: misses[k] for k in keep}
    elif conn_row is not None:
        if complete and not error:
            log("removal detection skipped: some requests or items failed, or the source returned nothing")
        state["docs"] = {**prev, **seen_keys}
    if conn_row is not None:
        state["last_counts"] = stats
        t.store.update_row(connectors_t, {"id": cid}, state=json.dumps(state))

    partial = bool(stats["failed"] or getattr(connector, "errors", 0) or (error and results))
    final = (
        "cancelled" if cancelled_flag[0] else "failed" if error and not results else "partial" if partial else "done"
    )
    log(f"{final}: {json.dumps(stats)}")
    t.store.update_row(ingest_jobs, {"id": jid}, status=final, error=error, finished_at=time.time())
    flush(force=True)
    t.audit.record(
        job["created_by"],
        "ingest_job_finished",
        job_id=jid,
        connector_id=cid,
        status=final,
        **{k: v for k, v in stats.items() if v},
    )
    return job_view(t.store.rows(ingest_jobs, id=jid)[0], with_items=True)


def claim_next(t: Any) -> int | None:
    for row in t.store.rows(ingest_jobs, order_by="id", desc=False, limit=5, tenant=t.id, status="queued"):
        if t.store.update_row(
            ingest_jobs,
            {"id": row["id"], "status": "queued"},
            status="running",
            started_at=time.time(),
            heartbeat=time.time(),
        ):
            return int(row["id"])  # won the race against other workers
    return None


def reap_stale(t: Any, older_than_s: float = 900) -> None:
    """Jobs whose worker died mid-run are marked failed so they can be re-run."""
    cutoff = time.time() - older_than_s
    for row in t.store.rows(ingest_jobs, tenant=t.id, status="running") + t.store.rows(
        ingest_jobs, tenant=t.id, status="cancelling"
    ):
        if (row["heartbeat"] or 0) < cutoff:
            t.store.update_row(
                ingest_jobs,
                {"id": row["id"]},
                status="failed",
                error="worker stopped while running",
                finished_at=time.time(),
            )


def enqueue_scheduled(t: Any, now: Any) -> None:
    from ..watch import cron_match

    for row in t.store.rows(connectors_t, tenant=t.id):
        if row["enabled"] and row["schedule"] and cron_match(row["schedule"], now):
            enqueue(t, row["created_by"], connector_id=row["id"], kind=row["kind"], title=f"Scheduled: {row['name']}")


class JobRunner(threading.Thread):
    """Executes queued ingestion jobs, one at a time per process. Several
    workers can run side by side; claiming a job is an atomic update."""

    def __init__(self, tenants: dict[str, Any], box: Any, poll_s: float = 1.0):
        super().__init__(daemon=True, name="smriti-jobs")
        self.tenants, self.box, self.poll_s = tenants, box, poll_s
        self._halt = threading.Event()  # not _stop: that name belongs to threading.Thread

    def run(self) -> None:
        last_reap = 0.0
        while not self._halt.is_set():
            worked = False
            for t in self.tenants.values():
                try:
                    if time.time() - last_reap > 60:
                        reap_stale(t)
                    jid = claim_next(t)
                    if jid is not None:
                        worked = True
                        run_job(t, self.box, jid)
                except Exception as e:
                    print(f"job runner: {type(e).__name__}: {e}", flush=True)
            if time.time() - last_reap > 60:
                last_reap = time.time()
            if not worked:
                self._halt.wait(self.poll_s)

    def stop(self) -> None:
        self._halt.set()


def run_pending(tenants: Iterable[Any], box: Any) -> list[dict[str, Any]]:
    """Run every queued job now, in this thread (tests, CLI)."""
    done = []
    for t in tenants:
        while (jid := claim_next(t)) is not None:
            done.append(run_job(t, box, jid))
    return done
