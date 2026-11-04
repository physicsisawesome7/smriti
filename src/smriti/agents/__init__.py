"""Agents: saved assistants with their own instructions, sources and tools.

Four kinds:

    classic   cited question answering over the agent's sources (the ask pipeline)
    agentic   a tool-using loop: search, calculate, call APIs, MCP servers, SQL, code
    research  plans sub-questions, answers each with verified citations, writes a report
    workflow  a graph of steps (agents, conditions, code, approvals, loops); see ``workflow``

Whoever asks, an agent only ever sees what that person may see: sources narrow
retrieval, never widen it, and configured tools carry their own role lists. An
API key acts as the person who created it.

This module holds definitions, tool configurations, conversations and API
keys; ``runtime`` runs agents.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from typing import Any

from ..models import Principal
from ..store import agents as agents_t
from ..store import api_keys, conversations, messages, tool_configs
from .tools import BUILTIN, CONFIGURABLE, Tool, ToolCtx, ToolError, catalog

KINDS = {
    "classic": "Cited answers from the agent's sources. Fast: lookups are answered without the GPU.",
    "agentic": "Uses tools in a loop: searches, calculates, calls APIs, MCP servers and databases, runs code.",
    "research": "Breaks a question into sub-questions, answers each with verified citations, and writes a report.",
    "workflow": "A graph of steps: agents, conditions, variables, code, human approvals and loops, run in order.",
}
APPROVAL = ("mutating", "always", "never")
DEFAULTS: dict[str, Any] = {
    "description": "",
    "instructions": "",
    "sources": {"mode": "all", "doc_types": [], "connectors": [], "doc_ids": []},
    "tools": ["search"],
    "approval": "mutating",
    "max_steps": 6,
    "starters": [],
    "roles": [],
    "schedule_prompt": "",
    "require_citations": True,
    "graph": None,
}

__all__ = ["KINDS", "AgentError", "Tool", "ToolCtx", "ToolError", "catalog"]


class AgentError(ValueError):
    pass


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    return s or "agent"


# ---------------------------------------------------------------- definitions


def view(row: dict[str, Any]) -> dict[str, Any]:
    cfg = {**DEFAULTS, **json.loads(row["config"] or "{}")}
    return {
        "id": row["agent_id"],
        "name": row["name"],
        "kind": row["kind"],
        "owner": row["owner"],
        "visibility": row["visibility"],
        "status": row["status"],
        "schedule": row["schedule"],
        "updated_at": row["updated_at"],
        **cfg,
    }


def normalise(body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind", "classic")
    if kind not in KINDS:
        raise AgentError(f"unknown agent kind '{kind}'")
    cfg = {k: body.get(k, v) for k, v in DEFAULTS.items()}
    if cfg["approval"] not in APPROVAL:
        raise AgentError(f"approval must be one of {', '.join(APPROVAL)}")
    src = {**DEFAULTS["sources"], **(cfg["sources"] or {})}
    if src["mode"] not in ("all", "selected"):
        raise AgentError("sources.mode must be 'all' or 'selected'")
    cfg["sources"] = src
    tools = []
    for ref in cfg["tools"] or []:
        ref = str(ref)
        if ref in BUILTIN or re.fullmatch(r"tool:\d+", ref):
            tools.append(ref)
        else:
            raise AgentError(f"unknown tool '{ref}'")
    cfg["tools"] = list(dict.fromkeys(tools))
    cfg["max_steps"] = max(1, min(int(cfg["max_steps"] or 6), 12))
    cfg["starters"] = [str(s)[:300] for s in (cfg["starters"] or []) if str(s).strip()][:6]
    cfg["roles"] = [str(r) for r in cfg["roles"] or []]
    cfg["instructions"] = str(cfg["instructions"] or "")[:8000]
    if kind == "workflow":
        from .workflow import WorkflowError, validate

        try:
            cfg["graph"] = validate(cfg["graph"])
        except WorkflowError as e:
            raise AgentError(str(e)) from e
        # The graph's agent steps carry their own tools.
        cfg["tools"] = sorted({ref for n in cfg["graph"]["nodes"] for ref in n["data"].get("tools", [])})
    else:
        cfg["graph"] = None
    return {"kind": kind, "config": cfg}


def save(t: Any, p: Principal, body: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise AgentError("name is required")
    norm = normalise(body)
    visibility = body.get("visibility", "private")
    if visibility not in ("private", "tenant", "roles"):
        raise AgentError("visibility must be private, tenant or roles")
    if visibility != "private" and not (p.can("curate") or p.can("administer")):
        raise PermissionError("sharing an agent needs the curate or administer grant")
    for ref in norm["config"]["tools"]:
        if ref.startswith("tool:"):
            row = tool_row(t, int(ref[5:]))  # must exist in this tenant
            cfg = json.loads(row["config"] or "{}")
            roles = [r.strip() for r in str(cfg.get("allowed_roles") or "").split(",") if r.strip()]
            if not (p.can("curate") or p.can("administer") or set(roles) & set(p.roles)):
                raise PermissionError(f"you are not allowed to use the tool '{row['name']}'")
    schedule = (body.get("schedule") or "").strip() or None
    from ..watch import valid_cron

    if schedule and not valid_cron(schedule):
        raise AgentError("schedule must be a 5-field cron expression")
    status = body.get("status", "draft") if body.get("status") in ("draft", "published") else "draft"
    now = time.time()
    if agent_id is None:
        base = _slug(name)
        agent_id = base
        n = 2
        while t.store.rows(agents_t, tenant=t.id, agent_id=agent_id):
            agent_id = f"{base}-{n}"
            n += 1
        t.store.insert(
            agents_t,
            tenant=t.id,
            agent_id=agent_id,
            name=name,
            kind=norm["kind"],
            config=json.dumps(norm["config"], ensure_ascii=False),
            owner=p.user_id,
            visibility=visibility,
            status=status,
            schedule=schedule,
            created_at=now,
            updated_at=now,
        )
        t.audit.record(p.user_id, "agent_create", agent_id=agent_id, kind=norm["kind"], tools=norm["config"]["tools"])
    else:
        row = get_row(t, agent_id)
        if not can_edit(p, row):
            raise PermissionError("only the owner or a curator can change this agent")
        t.store.update_row(
            agents_t,
            {"tenant": t.id, "agent_id": agent_id},
            name=name,
            kind=norm["kind"],
            config=json.dumps(norm["config"], ensure_ascii=False),
            visibility=visibility,
            status=status,
            schedule=schedule,
            updated_at=now,
        )
        t.audit.record(p.user_id, "agent_update", agent_id=agent_id, kind=norm["kind"], tools=norm["config"]["tools"])
    return view(get_row(t, agent_id))


def get_row(t: Any, agent_id: str) -> dict[str, Any]:
    rows = t.store.rows(agents_t, tenant=t.id, agent_id=agent_id)
    if not rows:
        raise KeyError(f"no agent {agent_id}")
    return rows[0]


def can_edit(p: Principal, row: dict[str, Any]) -> bool:
    return row["owner"] == p.user_id or p.can("curate") or p.can("administer")


def can_use(p: Principal, row: dict[str, Any]) -> bool:
    if row["owner"] == p.user_id:
        return True
    if row["status"] != "published":
        return False
    if row["visibility"] == "tenant":
        return True
    if row["visibility"] == "roles":
        roles = json.loads(row["config"] or "{}").get("roles") or []
        return bool(set(roles) & set(p.roles))
    return False


def get(t: Any, p: Principal, agent_id: str) -> dict[str, Any]:
    row = get_row(t, agent_id)
    if not (can_use(p, row) or can_edit(p, row)):
        raise PermissionError("you cannot use this agent")
    return view(row) | {"can_edit": can_edit(p, row)}


def list_for(t: Any, p: Principal) -> list[dict[str, Any]]:
    out = []
    for row in t.store.rows(agents_t, order_by="updated_at", tenant=t.id):
        if can_use(p, row) or (row["owner"] == p.user_id):
            out.append(view(row) | {"can_edit": can_edit(p, row)})
        elif p.can("curate") or p.can("administer"):
            out.append(view(row) | {"can_edit": True})
    return out


def delete(t: Any, p: Principal, agent_id: str) -> None:
    row = get_row(t, agent_id)
    if not can_edit(p, row):
        raise PermissionError("only the owner or a curator can delete this agent")
    t.store.delete_rows(agents_t, tenant=t.id, agent_id=agent_id)
    for k in t.store.rows(api_keys, tenant=t.id, agent_id=agent_id):
        t.store.update_row(api_keys, {"id": k["id"]}, revoked=True)
    t.audit.record(p.user_id, "agent_delete", agent_id=agent_id)


def resolve_scope(t: Any, cfg: dict[str, Any]) -> list[str] | None:
    """The agent's documents as ids, or None for "everything the asker may see"."""
    src = cfg.get("sources") or {}
    if src.get("mode") != "selected":
        return None
    types_, conns, ids = (
        set(src.get("doc_types") or []),
        set(src.get("connectors") or []),
        set(src.get("doc_ids") or []),
    )
    out = []
    for d in t.store.list_documents(t.id):
        source = d.get("source") or ""
        if (
            d["doc_id"] in ids
            or d["doc_type"] in types_
            or (source.startswith("connector:") and source[10:] in conns)
            or source in conns
        ):
            out.append(d["doc_id"])
    return out


# ---------------------------------------------------------------- tool configurations


def tool_view(row: dict[str, Any], box: Any) -> dict[str, Any]:
    cls = CONFIGURABLE.get(row["kind"])
    acts = json.loads(row["actions"] or "[]")
    return {
        "id": row["id"],
        "ref": f"tool:{row['id']}",
        "kind": row["kind"],
        "label": cls.label if cls else row["kind"],
        "icon": cls.icon if cls else "◇",
        "name": row["name"],
        "config": json.loads(row["config"] or "{}"),
        "secrets_set": sorted(box.open(row["secrets"]) if row["secrets"] else {}),
        "actions": [
            {"name": a["name"], "description": a["description"], "mutates": a.get("mutates", False)} for a in acts
        ],
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
    }


def tool_row(t: Any, tid: int) -> dict[str, Any]:
    rows = t.store.rows(tool_configs, tenant=t.id, id=tid)
    if not rows:
        raise KeyError(f"no tool {tid}")
    return rows[0]


def make_tool(t: Any, p: Principal, ref: str, box: Any, **ctx_kw: Any) -> Tool:
    if ref in BUILTIN:
        return BUILTIN[ref](ToolCtx(tenant=t, principal=p, name=BUILTIN[ref].label, **ctx_kw))
    row = tool_row(t, int(ref.split(":", 1)[1]))
    if not row["enabled"]:
        raise ValueError(f"tool {row['name']} is switched off")
    cls = CONFIGURABLE[row["kind"]]
    return cls(
        ToolCtx(
            tenant=t,
            principal=p,
            config=json.loads(row["config"] or "{}"),
            secrets=box.open(row["secrets"]) if row["secrets"] else {},
            actions_cache=json.loads(row["actions"] or "[]"),
            tool_id=row["id"],
            name=row["name"],
            **ctx_kw,
        )
    )


def tool_allowed(p: Principal, tool: Tool, agent: dict[str, Any] | None = None) -> bool:
    """Who may run a tool.

    Built-in tools follow their own grants (code execution needs ``execute_code``).
    A connected tool holds credentials, so it is denied by default: it runs for the
    roles it lists, or, when it lists none, only for curators and inside agents a
    curator has published."""
    if tool.kind == "code":
        return p.can("execute_code") or p.can("administer")
    if not tool.configurable:
        return True
    roles = [r.strip() for r in str(tool.cfg.get("allowed_roles") or "").split(",") if r.strip()]
    if roles:
        return bool(set(roles) & set(p.roles))
    if p.can("curate") or p.can("administer"):
        return True
    return bool(agent and agent.get("status") == "published" and agent.get("published_by_curator"))


def save_tool(t: Any, p: Principal, box: Any, body: dict[str, Any], tid: int | None = None) -> dict[str, Any]:
    if not (p.can("curate") or p.can("administer")):
        raise PermissionError("configuring tools needs the curate or administer grant")
    kind = body.get("kind", "")
    cls = CONFIGURABLE.get(kind)
    if not cls:
        raise AgentError(f"unknown tool kind '{kind}'")
    name = str(body.get("name") or cls.label).strip()[:200]
    values = body.get("values") or {}
    secret_names = {f["name"] for f in cls.fields if f["type"] == "secret"}
    known = {f["name"] for f in cls.fields}
    config = {k: v for k, v in values.items() if k in known and k not in secret_names}
    new_secrets = {k: v for k, v in values.items() if k in secret_names and v not in (None, "")}
    old = box.open(tool_row(t, tid)["secrets"]) if tid is not None and tool_row(t, tid)["secrets"] else {}
    sealed = {**old, **new_secrets}
    tool = cls(ToolCtx(tenant=t, principal=p, config=config, secrets=sealed, name=name))
    missing = [f["label"] for f in cls.fields if f["required"] and not (config.get(f["name"]) or sealed.get(f["name"]))]
    if missing:
        raise AgentError("missing: " + ", ".join(missing))
    acts = tool.discover() or [a.__dict__ for a in tool.actions()]
    now = time.time()
    if tid is None:
        tid = t.store.insert(
            tool_configs,
            tenant=t.id,
            kind=kind,
            name=name,
            config=json.dumps(config),
            secrets=box.seal(sealed),
            actions=json.dumps(acts),
            enabled=True,
            created_by=p.user_id,
            created_at=now,
            updated_at=now,
        )
        t.audit.record(
            p.user_id, "tool_create", tool_id=tid, kind=kind, actions=len(acts), secrets_set=sorted(new_secrets)
        )
    else:
        t.store.update_row(
            tool_configs,
            {"id": tid, "tenant": t.id},
            name=name,
            config=json.dumps(config),
            secrets=box.seal(sealed),
            actions=json.dumps(acts),
            updated_at=now,
        )
        t.audit.record(p.user_id, "tool_update", tool_id=tid, actions=len(acts), secrets_changed=sorted(new_secrets))
    return tool_view(tool_row(t, tid), box)


def delete_tool(t: Any, p: Principal, tid: int) -> None:
    if not (p.can("curate") or p.can("administer")):
        raise PermissionError("needs the curate or administer grant")
    tool_row(t, tid)
    t.store.delete_rows(tool_configs, tenant=t.id, id=tid)
    t.audit.record(p.user_id, "tool_delete", tool_id=tid)


# ---------------------------------------------------------------- conversations


def new_conversation(t: Any, user_id: str, agent_id: str | None, title: str) -> str:
    cid = uuid.uuid4().hex
    now = time.time()
    t.store.insert(
        conversations,
        id=cid,
        tenant=t.id,
        user_id=user_id,
        agent_id=agent_id,
        title=title[:120] or "New conversation",
        created_at=now,
        updated_at=now,
    )
    return cid


def conversation_row(t: Any, cid: str) -> dict[str, Any]:
    rows = t.store.rows(conversations, tenant=t.id, id=cid)
    if not rows:
        raise KeyError(f"no conversation {cid}")
    return rows[0]


def own_conversation(t: Any, p: Principal, cid: str) -> dict[str, Any]:
    row = conversation_row(t, cid)
    if row["user_id"] != p.user_id:
        raise PermissionError("not your conversation")
    return row


def add_message(t: Any, cid: str, role: str, content: str, data: dict[str, Any] | None = None) -> int:
    now = time.time()
    mid = t.store.insert(
        messages,
        conversation_id=cid,
        tenant=t.id,
        role=role,
        content=content,
        data=json.dumps(data or {}, ensure_ascii=False, default=str),
        ts=now,
    )
    t.store.update_row(conversations, {"id": cid, "tenant": t.id}, updated_at=now)
    return mid


def history(t: Any, cid: str, limit: int = 8) -> list[dict[str, str]]:
    rows = t.store.rows(messages, order_by="id", desc=True, limit=limit, conversation_id=cid)
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def conversation(t: Any, row: dict[str, Any]) -> dict[str, Any]:
    msgs = t.store.rows(messages, order_by="id", desc=False, conversation_id=row["id"])
    return {
        "id": row["id"],
        "title": row["title"],
        "agent_id": row["agent_id"],
        "user_id": row["user_id"],
        "shared": bool(row["share_token"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "messages": [
            {"id": m["id"], "role": m["role"], "content": m["content"], "ts": m["ts"], **json.loads(m["data"] or "{}")}
            for m in msgs
        ],
    }


def list_conversations(t: Any, p: Principal, agent_id: str | None = None) -> list[dict[str, Any]]:
    where: dict[str, Any] = {"tenant": t.id, "user_id": p.user_id}
    if agent_id:
        where["agent_id"] = agent_id
    rows = t.store.rows(conversations, order_by="updated_at", limit=100, **where)
    return [
        {k: r[k] for k in ("id", "title", "agent_id", "updated_at")} | {"shared": bool(r["share_token"])} for r in rows
    ]


def share(t: Any, p: Principal, cid: str, on: bool = True) -> str | None:
    own_conversation(t, p, cid)
    token = secrets.token_urlsafe(24) if on else None
    t.store.update_row(conversations, {"id": cid, "tenant": t.id}, share_token=token)
    t.audit.record(p.user_id, "conversation_share" if on else "conversation_unshare", conversation_id=cid)
    return token


def shared_view(t: Any, viewer: Principal, token: str) -> dict[str, Any]:
    """A shared conversation, as the viewer may see it.

    Sharing never widens access: an answer that cites a document the viewer
    cannot open is withheld from them.
    """
    rows = t.store.rows(conversations, tenant=t.id, share_token=token)
    if not rows:
        raise KeyError("no such shared conversation")
    conv = conversation(t, rows[0])
    visible = t.visible_docs(viewer)
    withheld = 0
    for m in conv["messages"]:
        cited = {c["doc_id"] for c in m.get("citations", []) if c.get("doc_type") != "tool-result"}
        if cited - visible:
            m.update(
                content="[withheld: this answer cites documents you cannot open]", citations=[], steps=[], withheld=True
            )
            withheld += 1
    t.audit.record(viewer.user_id, "conversation_view_shared", conversation_id=conv["id"], withheld=withheld)
    return conv | {"withheld": withheld, "owner": rows[0]["user_id"]}


# ---------------------------------------------------------------- API keys

KEY_PREFIX = "sk-smriti-"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(t: Any, p: Principal, agent_id: str, name: str) -> dict[str, Any]:
    row = get_row(t, agent_id)
    if not can_edit(p, row):
        raise PermissionError("only the owner or a curator can create keys for this agent")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    kid = t.store.insert(
        api_keys,
        tenant=t.id,
        agent_id=agent_id,
        name=name[:200] or "key",
        prefix=key[:18],
        key_hash=_hash(key),
        acts_as=p.user_id,
        created_by=p.user_id,
        created_at=time.time(),
        calls=0,
        revoked=False,
    )
    t.audit.record(p.user_id, "api_key_create", key_id=kid, agent_id=agent_id, acts_as=p.user_id)
    return {"id": kid, "key": key, "prefix": key[:18], "agent_id": agent_id, "acts_as": p.user_id}


def list_keys(t: Any, p: Principal, agent_id: str) -> list[dict[str, Any]]:
    row = get_row(t, agent_id)
    if not can_edit(p, row):
        raise PermissionError("only the owner or a curator can see this agent's keys")
    return [
        {
            k: r[k]
            for k in ("id", "name", "prefix", "acts_as", "created_by", "created_at", "last_used", "calls", "revoked")
        }
        for r in t.store.rows(api_keys, order_by="id", tenant=t.id, agent_id=agent_id)
    ]


def revoke_key(t: Any, p: Principal, kid: int) -> None:
    rows = t.store.rows(api_keys, tenant=t.id, id=kid)
    if not rows:
        raise KeyError(f"no key {kid}")
    if rows[0]["agent_id"]:
        row = get_row(t, rows[0]["agent_id"])
        if not can_edit(p, row):
            raise PermissionError("only the owner or a curator can revoke this key")
    t.store.update_row(api_keys, {"id": kid}, revoked=True)
    t.audit.record(p.user_id, "api_key_revoke", key_id=kid)


def lookup_key(store: Any, key: str) -> dict[str, Any] | None:
    if not key.startswith(KEY_PREFIX):
        return None
    rows = store.rows(api_keys, key_hash=_hash(key))
    if not rows or rows[0]["revoked"]:
        return None
    r = rows[0]
    store.update_row(api_keys, {"id": r["id"]}, last_used=time.time(), calls=int(r["calls"] or 0) + 1)
    return r
