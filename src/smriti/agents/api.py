"""HTTP routes for agents, tools, conversations, approvals and API keys.

Agent API keys (``sk-smriti-…``) authenticate only here and on
``/v1/chat/completions``. A key is bound to one agent and acts as the person
who created it, so it can never read more than they can.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from ..models import Principal
from ..store import artifacts
from . import (
    KEY_PREFIX,
    KINDS,
    AgentError,
    catalog,
    conversation,
    create_key,
    delete,
    delete_tool,
    get,
    list_conversations,
    list_for,
    list_keys,
    lookup_key,
    normalise,
    own_conversation,
    revoke_key,
    save,
    save_tool,
    share,
    shared_view,
    tool_row,
    tool_view,
)
from . import runtime as rt
from . import workflow as wf
from .expr import FUNCS
from .tools import ToolError

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatBody(BaseModel):
    question: str
    conversation_id: str | None = None
    stream: bool = False


class PreviewBody(BaseModel):
    agent: dict[str, Any]
    question: str


class ToolBody(BaseModel):
    kind: str
    name: str = ""
    values: dict[str, Any] = {}


class KeyBody(BaseModel):
    name: str = "key"


def sse(events: Iterator[dict[str, Any]]) -> StreamingResponse:
    def gen() -> Iterator[str]:
        try:
            for ev in events:
                if ev["event"] == "final" and (ev["data"].get("model") or "").startswith(("extractive", "computed")):
                    for i, w in enumerate(ev["data"]["answer"].split(" ")):  # offline writer: word-by-word for the UI
                        yield f"event: token\ndata: {json.dumps((' ' if i else '') + w)}\n\n"
                        time.sleep(0.004)
                yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False, default=str)}\n\n"
        except (PermissionError, KeyError, ValueError) as e:
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


def mount(
    app: FastAPI, plat: Any, ctx: Callable[..., tuple[Any, Principal]], need: Callable[..., None]
) -> Callable[..., Any]:
    box = plat.box

    def errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'")) from None
        except PermissionError as e:
            raise HTTPException(403, str(e)) from None
        except (AgentError, ToolError, ValueError) as e:
            raise HTTPException(400, str(e)) from None

    def key_or_user(
        request: Request,
        authorization: str | None = Header(default=None),
        x_smriti_user: str | None = Header(default=None),
        x_smriti_tenant: str | None = Header(default=None),
    ) -> tuple[Any, Principal, dict[str, Any] | None]:
        tok = (authorization or "").removeprefix("Bearer ").strip()
        if tok.startswith(KEY_PREFIX):
            for t in plat.tenants.values():
                k = lookup_key(t.store, tok)
                if k and k["tenant"] == t.id:
                    try:
                        return t, t.principal(k["acts_as"]), k
                    except Exception:
                        raise HTTPException(401, "the key's owner is no longer in the directory") from None
            raise HTTPException(401, "invalid or revoked API key")
        t, p = ctx(request, x_smriti_user, x_smriti_tenant, authorization)
        return t, p, None

    def agent_allowed(k: dict[str, Any] | None, agent_id: str) -> None:
        if k is not None and k["agent_id"] != agent_id:
            raise HTTPException(403, "this API key is for another agent")

    # ------------------------------------------------------------ catalogue and definitions

    @app.get("/api/agents/catalog")
    def agents_catalog(c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        need(p, "view")
        return {
            "kinds": [{"kind": k, "description": v} for k, v in KINDS.items()],
            "workflow": {
                "nodes": [{"type": k, "label": v} for k, v in wf.NODE_TYPES.items()],
                "templates": wf.TEMPLATES,
                "functions": sorted(FUNCS),
                "max_steps": wf.MAX_STEPS,
            },
            "tools": catalog(),
            "configured": [tool_view(r, box) for r in _tools(t)],
            "model": t.gw.describe()["routes"]["chat"],
            "can_share": p.can("curate") or p.can("administer"),
            "roles": sorted(t.pack.roles.roles),
            "doc_types": sorted({d["doc_type"] for d in t.store.list_documents(t.id)}),
            "connectors": sorted(
                {
                    (d.get("source") or "")[10:]
                    for d in t.store.list_documents(t.id)
                    if (d.get("source") or "").startswith("connector:")
                }
            ),
        }

    def _tools(t: Any) -> list[dict[str, Any]]:
        from ..store import tool_configs

        return t.store.rows(tool_configs, order_by="id", desc=False, tenant=t.id)

    @app.get("/api/agents")
    def agents_list(c: tuple[Any, Principal] = Depends(ctx)) -> list[dict[str, Any]]:
        t, p = c
        need(p, "view")
        return list_for(t, p)

    @app.post("/api/agents")
    def agents_create(body: dict[str, Any], c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        need(p, "view")
        return errors(lambda: save(t, p, body))

    @app.get("/api/agents/{agent_id}")
    def agents_get(agent_id: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        return errors(lambda: get(t, p, agent_id))

    @app.put("/api/agents/{agent_id}")
    def agents_update(agent_id: str, body: dict[str, Any], c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        return errors(lambda: save(t, p, body, agent_id))

    @app.delete("/api/agents/{agent_id}")
    def agents_delete(agent_id: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        errors(lambda: delete(t, p, agent_id))
        return {"deleted": agent_id}

    # ------------------------------------------------------------ running

    @app.post("/api/agents/{agent_id}/chat")
    def agents_chat(agent_id: str, body: ChatBody, c: tuple[Any, Principal, Any] = Depends(key_or_user)) -> Any:
        t, p, k = c
        need(p, "view")
        agent_allowed(k, agent_id)
        agent = errors(lambda: rt.agent_for(t, p, agent_id))
        if body.conversation_id:
            errors(lambda: own_conversation(t, p, body.conversation_id))
        events = rt.chat(
            t, p, box, agent, body.question, conversation_id=body.conversation_id, trigger="api" if k else "chat"
        )
        if body.stream:
            return sse(events)
        return errors(lambda: rt.run_sync(events))

    @app.post("/api/agents/preview")
    def agents_preview(body: PreviewBody, c: tuple[Any, Principal] = Depends(ctx)) -> StreamingResponse:
        """Try an agent before saving it. Nothing is stored except the run itself (for approvals)."""
        t, p = c
        need(p, "view")
        norm = errors(lambda: normalise(body.agent))
        agent = {**norm["config"], "kind": norm["kind"], "id": None, "name": body.agent.get("name") or "Preview"}
        for ref in agent["tools"]:
            if ref.startswith("tool:"):
                errors(lambda ref=ref: tool_row(t, int(ref[5:])))
        return sse(rt.chat(t, p, box, agent, body.question, trigger="preview", keep=False))

    @app.post("/api/runs/{run_id}/approve")
    def runs_approve(
        run_id: str, approve: bool, stream: bool = True, c: tuple[Any, Principal, Any] = Depends(key_or_user)
    ) -> Any:
        t, p, _k = c
        errors(lambda: rt.Run.load(t, p, box, run_id))
        events = rt.approve(t, p, box, run_id, approve)
        return sse(events) if stream else errors(lambda: rt.run_sync(events))

    # ------------------------------------------------------------ conversations

    @app.get("/api/conversations")
    def convs(agent_id: str | None = None, c: tuple[Any, Principal] = Depends(ctx)) -> list[dict[str, Any]]:
        t, p = c
        return list_conversations(t, p, agent_id)

    @app.get("/api/conversations/{cid}")
    def conv_get(cid: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        return errors(lambda: conversation(t, own_conversation(t, p, cid)))

    @app.delete("/api/conversations/{cid}")
    def conv_delete(cid: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        from ..store import conversations, messages

        t, p = c
        errors(lambda: own_conversation(t, p, cid))
        t.store.delete_rows(messages, conversation_id=cid)
        t.store.delete_rows(conversations, tenant=t.id, id=cid)
        t.audit.record(p.user_id, "conversation_delete", conversation_id=cid)
        return {"deleted": cid}

    @app.post("/api/conversations/{cid}/share")
    def conv_share(cid: str, on: bool = True, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        token = errors(lambda: share(t, p, cid, on))
        return {"token": token, "path": f"/?shared={token}" if token else None}

    @app.get("/api/shared/{token}")
    def conv_shared(token: str, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        need(p, "view")
        return errors(lambda: shared_view(t, p, token))

    @app.get("/api/artifacts/{aid}")
    def artifact_get(aid: str, c: tuple[Any, Principal] = Depends(ctx)) -> Response:
        t, p = c
        rows = t.store.rows(artifacts, tenant=t.id, id=aid)
        if not rows or rows[0]["user_id"] != p.user_id:
            raise HTTPException(404, "no such file")
        a = rows[0]
        safe = a["filename"].replace('"', "")
        # downloads only: an agent-written HTML file never runs in Smriti's origin
        return Response(
            t.blobs.get(a["blob_key"]),
            media_type=a["mime"] if not a["mime"].startswith("text/html") else "text/plain",
            headers={"Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff"},
        )

    # ------------------------------------------------------------ tools

    @app.get("/api/tools")
    def tools_list(c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        need(p, "view")
        return {
            "catalog": catalog(),
            "configured": [tool_view(r, box) for r in _tools(t)],
            "can_manage": p.can("curate") or p.can("administer"),
        }

    @app.get("/api/tools/presets")
    def tools_presets(request: Request, c: tuple[Any, Principal] = Depends(ctx)) -> list[dict[str, Any]]:
        """Tools pointing at Smriti's own demo targets, as the server reaches them."""
        import os

        base = (os.environ.get("SMRITI_SELF_URL") or str(request.base_url)).rstrip("/")
        db = (plat.settings.data_dir / "imports" / "db" / "works.db").resolve().as_posix()
        ops = [
            {
                "name": "list_notices",
                "method": "GET",
                "path": "/notices",
                "description": "Tender and works notices, newest first. Paginated.",
                "params": {
                    "type": "object",
                    "properties": {"page": {"type": "integer"}, "per_page": {"type": "integer"}},
                    "required": [],
                },
            }
        ]
        return [
            {
                "kind": "mcp",
                "name": "Handover notes (demo MCP server)",
                "values": {"url": f"{base}/demo-mcp/mcp", "doc_class": "internal"},
            },
            {
                "kind": "rest_api",
                "name": "Notices API (demo)",
                "values": {
                    "base_url": f"{base}/demo-api",
                    "operations": json.dumps(ops, indent=1),
                    "doc_class": "internal",
                },
            },
            {
                "kind": "sql",
                "name": "Works register (demo SQLite)",
                "values": {
                    "url": f"sqlite:///{db}",
                    "schema_notes": "works_register(id, work_name, division, sanctioned_cost_cr, status, remarks, updated_at)",
                    "doc_class": "internal",
                },
            },
        ]

    @app.post("/api/tools")
    def tools_create(body: ToolBody, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        return errors(lambda: save_tool(t, p, box, body.model_dump()))

    @app.put("/api/tools/{tid}")
    def tools_update(tid: int, body: ToolBody, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        errors(lambda: tool_row(t, tid))
        return errors(lambda: save_tool(t, p, box, body.model_dump(), tid))

    @app.delete("/api/tools/{tid}")
    def tools_delete(tid: int, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        errors(lambda: delete_tool(t, p, tid))
        return {"deleted": tid}

    # ------------------------------------------------------------ keys

    @app.get("/api/agents/{agent_id}/keys")
    def keys_list(agent_id: str, c: tuple[Any, Principal] = Depends(ctx)) -> list[dict[str, Any]]:
        t, p = c
        return errors(lambda: list_keys(t, p, agent_id))

    @app.post("/api/agents/{agent_id}/keys")
    def keys_create(agent_id: str, body: KeyBody, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        return errors(lambda: create_key(t, p, agent_id, body.name))

    @app.delete("/api/keys/{kid}")
    def keys_revoke(kid: int, c: tuple[Any, Principal] = Depends(ctx)) -> dict[str, Any]:
        t, p = c
        errors(lambda: revoke_key(t, p, kid))
        return {"revoked": kid}

    return key_or_user
