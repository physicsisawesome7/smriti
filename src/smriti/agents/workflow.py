"""Workflows: agents built as a graph of steps.

A workflow is an agent of kind ``workflow`` whose config holds a ``graph``:

    {"nodes": [{"id", "type", "title", "x", "y", "data"}],
     "edges": [{"source", "target", "handle"}]}

Node types (``handle`` names the outgoing edge a node takes):

    start      where every run begins; variables ``query`` and ``chat_history``
    agent      a classic, agentic or research agent on a templated prompt;
               writes its answer to a variable, its citations to the run   out
    condition  branches on expressions, first match wins          b0, b1, ..., else
    set_state  assigns variables from expressions                         out
    code       Python in the sandbox; reads ``state``, prints its result  out
    approval   pauses until the person approves or declines    approved, declined
    loop       runs one agent prompt per item of a list, collecting answers out
    end        renders the final answer from a template
    note       a comment on the canvas; never runs

Every node's result is also stored as ``node_<id>``, and the latest agent
answer as ``last``. Templates use ``{{ expression }}`` and expressions use the
safe language in ``expr``.

The usual guarantees hold inside a workflow: each agent step runs as the
person asking, masks personal data before the model and verifies its answer;
a tool action that needs approval pauses the whole workflow; and the
workflow's sources narrow every agent step. A run stops after MAX_STEPS
steps, so a cycle cannot run forever.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..models import Passage, Principal
from . import sandbox
from .expr import ExprError, evaluate, names, render
from .runtime import Run, _empty, _renumber

MAX_STEPS = 50
MAX_NODES = 60
MAX_LOOP = 20
NODE_TYPES = {
    "start": "Start",
    "agent": "AI agent",
    "condition": "If / else",
    "set_state": "Set state",
    "code": "Code",
    "approval": "Human approval",
    "loop": "Loop",
    "end": "End",
    "note": "Note",
}
AGENT_KINDS = ("classic", "agentic", "research")
VAR = r"[A-Za-z_][A-Za-z0-9_]{0,40}"


class WorkflowError(ValueError):
    pass


# ---------------------------------------------------------------- validation


def _handles(node: dict[str, Any]) -> list[str]:
    t = node["type"]
    if t == "condition":
        return [f"b{i}" for i in range(len(node["data"].get("branches") or []))] + ["else"]
    if t == "approval":
        return ["approved", "declined"]
    if t in ("end", "note"):
        return []
    return ["out"]


def validate(graph: Any) -> dict[str, Any]:
    """Checks a graph and returns a cleaned copy. Raises WorkflowError with a readable reason."""
    import re

    from . import normalise

    if not isinstance(graph, dict):
        raise WorkflowError("a workflow needs a graph with nodes and edges")
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise WorkflowError("graph.nodes and graph.edges must be lists")
    if len(nodes) > MAX_NODES:
        raise WorkflowError(f"a workflow can have at most {MAX_NODES} nodes")
    clean_nodes: list[dict[str, Any]] = []
    ids: dict[str, dict[str, Any]] = {}
    for raw in nodes:
        nid = str(raw.get("id") or "").strip()
        t = raw.get("type")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", nid):
            raise WorkflowError(f"node id '{nid}' must be 1-40 letters, digits, - or _")
        if nid in ids:
            raise WorkflowError(f"two nodes share the id '{nid}'")
        if t not in NODE_TYPES:
            raise WorkflowError(f"node '{nid}' has unknown type '{t}'")
        data = dict(raw.get("data") or {})
        node = {
            "id": nid,
            "type": t,
            "title": str(raw.get("title") or NODE_TYPES[t])[:80],
            "x": float(raw.get("x") or 0),
            "y": float(raw.get("y") or 0),
            "data": _clean_data(nid, t, data, normalise, re),
        }
        ids[nid] = node
        clean_nodes.append(node)
    starts = [n for n in clean_nodes if n["type"] == "start"]
    if len(starts) != 1:
        raise WorkflowError("a workflow needs exactly one Start node")
    if not any(n["type"] == "end" for n in clean_nodes):
        raise WorkflowError("a workflow needs at least one End node")
    clean_edges = []
    seen: set[tuple[str, str]] = set()
    for e in edges:
        s, d, h = str(e.get("source") or ""), str(e.get("target") or ""), str(e.get("handle") or "out")
        if s not in ids or d not in ids:
            raise WorkflowError(f"an edge connects a missing node ({s} → {d})")
        if h not in _handles(ids[s]):
            raise WorkflowError(f"'{ids[s]['title']}' has no output called '{h}'")
        if ids[d]["type"] in ("start", "note"):
            raise WorkflowError(f"nothing can connect into '{ids[d]['title']}'")
        if (s, h) in seen:
            raise WorkflowError(f"'{ids[s]['title']}' output '{h}' is connected twice")
        seen.add((s, h))
        clean_edges.append({"source": s, "target": d, "handle": h})
    for n in clean_nodes:
        for h in _handles(n):
            if (n["id"], h) not in seen:
                label = "" if h == "out" else f" ({_handle_label(n, h)})"
                raise WorkflowError(f"'{n['title']}'{label} is not connected to a next step")
    # Every runnable node must be reachable from Start.
    nxt: dict[str, list[str]] = {}
    for e in clean_edges:
        nxt.setdefault(e["source"], []).append(e["target"])
    reach, todo = set(), [starts[0]["id"]]
    while todo:
        cur = todo.pop()
        if cur in reach:
            continue
        reach.add(cur)
        todo += nxt.get(cur, [])
    lost = [n["title"] for n in clean_nodes if n["type"] != "note" and n["id"] not in reach]
    if lost:
        raise WorkflowError(f"not reachable from Start: {', '.join(lost)}")
    return {"nodes": clean_nodes, "edges": clean_edges}


def _handle_label(node: dict[str, Any], h: str) -> str:
    if h.startswith("b") and h[1:].isdigit():
        b = node["data"]["branches"][int(h[1:])]
        return b.get("label") or f"branch {int(h[1:]) + 1}"
    return h


def _check_expr(nid: str, src: str) -> None:
    try:
        evaluate(src, {})
    except ExprError as e:
        if "not allowed" in str(e) or "invalid" in str(e) or "unknown function" in str(e) or "too long" in str(e):
            raise WorkflowError(f"node '{nid}': {e}") from e
        # Other errors (missing values, wrong types) only show up with real data.


def _check_template(nid: str, tpl: str) -> None:
    import re

    for m in re.finditer(r"\{\{\s*(.+?)\s*\}\}", tpl or "", re.S):
        _check_expr(nid, m.group(1))


def _clean_data(nid: str, t: str, d: dict[str, Any], normalise: Any, re: Any) -> dict[str, Any]:
    if t == "agent" or t == "loop":
        kind = d.get("kind", "classic")
        if kind not in AGENT_KINDS:
            raise WorkflowError(f"node '{nid}': agent kind must be one of {', '.join(AGENT_KINDS)}")
        sub = normalise(
            {
                "kind": kind,
                "instructions": d.get("instructions", ""),
                "tools": d.get("tools") or ["search"],
                "max_steps": d.get("max_steps", 6),
            }
        )["config"]
        out = {
            "kind": kind,
            "prompt": str(d.get("prompt") or ("{{ item }}" if t == "loop" else "{{ query }}"))[:8000],
            "instructions": sub["instructions"],
            "tools": sub["tools"],
            "max_steps": sub["max_steps"],
            "output": str(d.get("output") or ("results" if t == "loop" else "answer")),
        }
        if t == "loop":
            out["items"] = str(d.get("items") or "").strip()
            out["item_var"] = str(d.get("item_var") or "item")
            if not out["items"]:
                raise WorkflowError(f"node '{nid}': a loop needs an items expression")
            _check_expr(nid, out["items"])
            if not re.fullmatch(VAR, out["item_var"]):
                raise WorkflowError(f"node '{nid}': '{out['item_var']}' is not a valid variable name")
        if not re.fullmatch(VAR, out["output"]):
            raise WorkflowError(f"node '{nid}': '{out['output']}' is not a valid variable name")
        _check_template(nid, out["prompt"])
        return out
    if t == "condition":
        branches = []
        for b in d.get("branches") or []:
            expr = str(b.get("expr") or "").strip()
            if not expr:
                raise WorkflowError(f"node '{nid}': every branch needs a condition")
            _check_expr(nid, expr)
            branches.append({"label": str(b.get("label") or "")[:40], "expr": expr})
        if not branches:
            raise WorkflowError(f"node '{nid}': an If / else needs at least one condition")
        return {"branches": branches[:8]}
    if t == "set_state":
        assign = []
        for a in d.get("assign") or []:
            name = str(a.get("name") or "").strip()
            if not re.fullmatch(VAR, name) or name in ("query", "chat_history"):
                raise WorkflowError(f"node '{nid}': '{name}' is not a variable you can set")
            _check_expr(nid, str(a.get("expr") or ""))
            assign.append({"name": name, "expr": str(a["expr"])})
        return {"assign": assign}
    if t == "code":
        code = str(d.get("code") or "")
        if not code.strip():
            raise WorkflowError(f"node '{nid}': the code is empty")
        out_var = str(d.get("output") or "code_result")
        if not re.fullmatch(VAR, out_var):
            raise WorkflowError(f"node '{nid}': '{out_var}' is not a valid variable name")
        return {"code": code[:20000], "output": out_var}
    if t == "approval":
        msg = str(d.get("message") or "Continue?")[:2000]
        _check_template(nid, msg)
        return {"message": msg}
    if t == "end":
        tpl = str(d.get("output") or "{{ last }}")[:8000]
        _check_template(nid, tpl)
        return {"output": tpl}
    if t == "note":
        return {"text": str(d.get("text") or "")[:2000]}
    return {}


def variables_used(graph: dict[str, Any]) -> list[str]:
    """Variables a graph reads, for the builder's hints."""
    found: set[str] = set()
    for n in graph.get("nodes", []):
        d = n.get("data") or {}
        for src in [b.get("expr", "") for b in d.get("branches", [])] + [
            a.get("expr", "") for a in d.get("assign", [])
        ]:
            found |= names(src)
    return sorted(found)


# ---------------------------------------------------------------- running


class WorkflowRun(Run):
    """Runs a workflow graph. Shares persistence, audit and approvals with Run."""

    def _graph(self) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], str]]:
        g = self.agent["graph"]
        nodes = {n["id"]: n for n in g["nodes"]}
        nxt = {(e["source"], e["handle"]): e["target"] for e in g["edges"]}
        return nodes, nxt

    def start(self, question: str, hist: list[dict[str, str]] | None = None) -> Iterator[dict[str, Any]]:
        self.state["question"] = question
        verdict = self.t.guard.check_input(question)
        self.events += list(verdict.events)
        if not verdict.allowed:
            self.t.audit.record(
                self.p.user_id,
                "agent_blocked",
                run_id=self.id,
                agent_id=self.agent.get("id"),
                category=verdict.category,
            )
            yield self._final(verdict.message or "Blocked.", abstained=True, blocked=verdict.category, verify=False)
            return
        nodes, nxt = self._graph()
        start = next(n for n in nodes.values() if n["type"] == "start")
        self.state["wf"] = {
            "vars": {"query": question, "chat_history": hist or [], "last": ""},
            "node": nxt[(start["id"], "out")],
            "count": 0,
            "abstained": [],
        }
        self._trace(start, "done", "Started")
        yield {"event": "step", "data": self.steps[-1]}
        yield from self._advance()

    def resume(self, approve: bool) -> Iterator[dict[str, Any]]:
        wf = self.state.get("wf") or {}
        if self.state.get("status") != "awaiting_approval" or not (wf.get("child") or wf.get("waiting")):
            raise ValueError("this run is not waiting for an approval")
        nodes, nxt = self._graph()
        node = nodes[wf["node"]]
        self.t.audit.record(
            self.p.user_id,
            "agent_approval",
            run_id=self.id,
            agent_id=self.agent.get("id"),
            node=node["id"],
            approved=approve,
        )
        if wf.get("child"):
            child = Run.load(self.t, self.p, self.box, wf.pop("child"))
            done = yield from self._drive_child(node, child.resume(approve), child)
            if not done:
                return
        else:
            wf.pop("waiting")
            handle = "approved" if approve else "declined"
            wf["vars"][f"node_{node['id']}"] = handle
            self._trace(node, handle, "You approved." if approve else "You declined.")
            yield {"event": "step", "data": self.steps[-1]}
            wf["node"] = nxt[(node["id"], handle)]
        yield from self._advance()

    # ------------------------------------------------------------ the loop

    def _advance(self) -> Iterator[dict[str, Any]]:
        wf = self.state["wf"]
        nodes, nxt = self._graph()
        while True:
            wf["count"] += 1
            if wf["count"] > MAX_STEPS:
                yield self._stop(f"stopped after {MAX_STEPS} steps; check the workflow for a cycle")
                return
            node = nodes[wf["node"]]
            wf["node"] = node["id"]
            t, d, v = node["type"], node["data"], wf["vars"]
            try:
                if t == "end":
                    answer = render(d["output"], v).strip()
                    self._trace(node, "done", "Finished")
                    yield {"event": "step", "data": self.steps[-1]}
                    yield self._end(answer)
                    return
                if t == "agent":
                    child = self._child(node)
                    prompt = render(d["prompt"], v).strip() or v["query"]
                    done = yield from self._drive_child(node, child.start(prompt), child)
                    if not done:
                        return
                    continue  # _drive_child moved to the next node
                if t == "loop":
                    yield from self._loop(node)
                elif t == "condition":
                    handle = "else"
                    for i, b in enumerate(d["branches"]):
                        if evaluate(b["expr"], v):
                            handle = f"b{i}"
                            break
                    v[f"node_{node['id']}"] = handle
                    self._trace(node, "done", f"Took: {_handle_label(node, handle)}")
                    yield {"event": "step", "data": self.steps[-1]}
                    wf["node"] = nxt[(node["id"], handle)]
                    continue
                elif t == "set_state":
                    for a in d["assign"]:
                        v[a["name"]] = evaluate(a["expr"], v)
                    v[f"node_{node['id']}"] = {a["name"]: v[a["name"]] for a in d["assign"]}
                    self._trace(node, "done", "Set " + ", ".join(a["name"] for a in d["assign"]))
                elif t == "code":
                    self._code(node)
                elif t == "approval":
                    message = render(d["message"], v)
                    wf["waiting"] = True
                    self._trace(node, "waiting", "Waiting for you")
                    yield {"event": "step", "data": self.steps[-1]}
                    self.save("awaiting_approval")
                    self.t.audit.record(self.p.user_id, "agent_approval_requested", run_id=self.id, node=node["id"])
                    yield {
                        "event": "approval",
                        "data": {
                            "run_id": self.id,
                            "conversation_id": self.conversation_id,
                            "tool": self.agent.get("name") or "Workflow",
                            "action": node["title"],
                            "description": self.masker.restore(message)[:2000],
                            "arguments": {},
                            "reason": "this workflow asks a person before continuing",
                        },
                    }
                    return
                if t not in ("loop", "set_state", "code"):
                    raise WorkflowError(f"cannot run a {t} node")
                yield {"event": "step", "data": self.steps[-1]}
                wf["node"] = nxt[(node["id"], "out")]
            except (ExprError, WorkflowError, sandbox.SandboxUnavailable) as e:
                self._trace(node, "error", str(e))
                yield {"event": "step", "data": self.steps[-1]}
                yield self._stop(f"'{node['title']}' failed: {e}")
                return

    # ------------------------------------------------------------ agent steps

    def _child(self, node: dict[str, Any], prompt_kind: str | None = None) -> Run:
        d = node["data"]
        sub = {
            "id": None,
            "name": f"{self.agent.get('name') or 'Workflow'} · {node['title']}",
            "kind": prompt_kind or d["kind"],
            "instructions": d.get("instructions", ""),
            "tools": d.get("tools") or ["search"],
            "max_steps": d.get("max_steps", 6),
            "sources": self.agent.get("sources"),
            "approval": self.agent.get("approval", "mutating"),
            "require_citations": self.agent.get("require_citations", True),
        }
        return Run(self.t, self.p, sub, self.box, trigger="workflow", conversation_id=None)

    def _absorb(self, result: dict[str, Any]) -> str:
        """Moves a step's cited passages into this run and renumbers its answer to match."""
        mapping = {}
        for d in result.get("citations") or []:
            mapping[d["n"]] = self.add_evidence([Passage(**d)])[0].n
        return _renumber(result.get("answer") or "", mapping)

    def _drive_child(
        self, node: dict[str, Any], events: Iterator[dict[str, Any]], child: Run
    ) -> Iterator[dict[str, Any]]:
        """Runs an agent step. Returns True when it finished, False when it paused for approval."""
        wf = self.state["wf"]
        _, nxt = self._graph()
        for ev in events:
            if ev["event"] == "step":
                yield {"event": "step", "data": {**ev["data"], "node": node["id"], "node_title": node["title"]}}
            elif ev["event"] == "approval":
                wf["child"] = child.id
                self.save("awaiting_approval")
                yield {"event": "approval", "data": {**ev["data"], "run_id": self.id, "node": node["id"]}}
                return False
            elif ev["event"] == "final":
                r = ev["data"]
                answer = "" if r.get("abstained") else self._absorb(r)
                if r.get("abstained"):
                    wf["abstained"].append(node["id"])
                out = node["data"]["output"]
                wf["vars"][out] = answer
                wf["vars"]["last"] = answer
                wf["vars"][f"node_{node['id']}"] = {
                    "answer": answer,
                    "abstained": bool(r.get("abstained")),
                    "citations": len(r.get("citations") or []),
                }
                self.steps.append(
                    {
                        "type": "node",
                        "node": node["id"],
                        "node_type": "agent",
                        "title": node["title"],
                        "status": "abstained" if r.get("abstained") else "done",
                        "summary": (f"{out} = " + _short(answer)) if answer else "No supported answer",
                    }
                )
                yield {"event": "step", "data": self.steps[-1]}
                wf["node"] = nxt[(node["id"], "out")]
                return True
        return True

    def _loop(self, node: dict[str, Any]) -> Iterator[dict[str, Any]]:
        d, v = node["data"], self.state["wf"]["vars"]
        items = evaluate(d["items"], v)
        if isinstance(items, str):
            items = [s.strip() for s in items.splitlines() if s.strip()]
        if not isinstance(items, list):
            raise WorkflowError(f"items must be a list, got {type(items).__name__}")
        results = []
        for item in items[:MAX_LOOP]:
            child = self._child(node)
            prompt = render(d["prompt"], {**v, d["item_var"]: item}).strip()
            answer, paused = "", False
            for ev in child.start(prompt):
                if ev["event"] == "step":
                    yield {"event": "step", "data": {**ev["data"], "node": node["id"], "node_title": node["title"]}}
                elif ev["event"] == "approval":
                    paused = True  # actions needing approval are skipped inside a loop
                    break
                elif ev["event"] == "final" and not ev["data"].get("abstained"):
                    answer = self._absorb(ev["data"])
            results.append({"item": item, "answer": answer, "skipped_action": paused})
        v[d["output"]] = results
        v[f"node_{node['id']}"] = results
        note = f" (first {MAX_LOOP} of {len(items)})" if len(items) > MAX_LOOP else ""
        self._trace(node, "done", f"{len(results)} item{'s' if len(results) != 1 else ''}{note}")

    # ------------------------------------------------------------ code

    def _code(self, node: dict[str, Any]) -> None:
        d, v = node["data"], self.state["wf"]["vars"]
        state = {k: x for k, x in v.items() if k != "chat_history"}
        preamble = "import json\nstate = json.loads(" + repr(json.dumps(state, ensure_ascii=False, default=str)) + ")\n"
        out = sandbox.run(preamble + d["code"], timeout_s=20)
        if out.get("exit_code") != 0:
            err = (out.get("stderr") or "").strip().splitlines()[-1:] or ["no output"]
            raise WorkflowError(
                ("timed out" if out.get("timed_out") else "exit code " + str(out.get("exit_code"))) + f": {err[0]}"
            )
        text = (out.get("stdout") or "").strip()
        lines = text.splitlines()
        try:
            value: Any = json.loads(lines[-1]) if lines else ""
        except json.JSONDecodeError:
            value = text
        v[d["output"]] = value
        v[f"node_{node['id']}"] = value
        self._trace(node, "done", f"{d['output']} = {_short(json.dumps(value, ensure_ascii=False, default=str))}")

    # ------------------------------------------------------------ results

    def _trace(self, node: dict[str, Any], status: str, summary: str) -> None:
        self.steps.append(
            {
                "type": "node",
                "node": node["id"],
                "node_type": node["type"],
                "title": node["title"],
                "status": status,
                "summary": summary[:300],
            }
        )

    def _end(self, answer: str) -> dict[str, Any]:
        v = self.t.guard.verify(answer, self.evidence) if answer and self.evidence else _empty()
        abstained = not answer
        if abstained:
            answer = "The workflow finished without an answer. The steps above show where it stopped."
        return self._final(answer, abstained=abstained, verification=v, verify=False)

    def _stop(self, why: str) -> dict[str, Any]:
        self.events.append({"layer": "agent", "check": "workflow", "result": why})
        return self._final(f"The workflow stopped: {why}.", abstained=True, verify=False)


def _short(s: str, n: int = 140) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def load_run(t: Any, p: Principal, box: Any, run_id: str) -> Run:
    """Loads a paused run as the right class."""
    run = Run.load(t, p, box, run_id)
    if run.agent.get("kind") == "workflow":
        wr = WorkflowRun.__new__(WorkflowRun)
        wr.__dict__.update(run.__dict__)
        return wr
    return run


# ---------------------------------------------------------------- templates


def _n(nid: str, t: str, x: float, y: float, title: str | None = None, **data: Any) -> dict[str, Any]:
    return {"id": nid, "type": t, "title": title or NODE_TYPES[t], "x": x, "y": y, "data": data}


def _e(s: str, d: str, h: str = "out") -> dict[str, str]:
    return {"source": s, "target": d, "handle": h}


TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "answer-approve",
        "name": "Answer, then a person signs off",
        "description": "A research agent drafts a cited answer. If it found one, a person approves it before it is released.",
        "graph": {
            "nodes": [
                _n("start", "start", 40, 160),
                _n(
                    "draft",
                    "agent",
                    260,
                    160,
                    "Draft a cited answer",
                    kind="research",
                    prompt="{{ query }}",
                    output="draft",
                ),
                _n(
                    "found",
                    "condition",
                    500,
                    160,
                    "Found an answer?",
                    branches=[{"label": "yes", "expr": "len(draft) > 0"}],
                ),
                _n("ok", "approval", 740, 80, "Sign-off", message="Release this answer?\n\n{{ draft }}"),
                _n("done", "end", 980, 40, "Released", output="{{ draft }}"),
                _n("held", "end", 980, 160, "Held back", output="A reviewer held this answer back."),
                _n(
                    "none",
                    "end",
                    740,
                    280,
                    "No answer",
                    output="No supported answer was found in the library for: {{ query }}",
                ),
            ],
            "edges": [
                _e("start", "draft"),
                _e("draft", "found"),
                _e("found", "ok", "b0"),
                _e("found", "none", "else"),
                _e("ok", "done", "approved"),
                _e("ok", "held", "declined"),
            ],
        },
    },
    {
        "id": "loop-questions",
        "name": "Answer a list of questions",
        "description": "Put one question per line or separate them with ';'. Each is answered with citations, then collected into one reply.",
        "graph": {
            "nodes": [
                _n("start", "start", 40, 160),
                _n(
                    "split",
                    "set_state",
                    260,
                    160,
                    "Split the questions",
                    assign=[{"name": "questions", "expr": "[strip(q) for q in split(query, ';')]"}],
                ),
                _n(
                    "each",
                    "loop",
                    500,
                    160,
                    "Answer each",
                    kind="classic",
                    items="questions",
                    item_var="q",
                    prompt="{{ q }}",
                    output="answers",
                ),
                _n("end", "end", 740, 160, "Combine", output="{{ join(pluck(answers, 'answer'), '\\n\\n') }}"),
            ],
            "edges": [_e("start", "split"), _e("split", "each"), _e("each", "end")],
        },
    },
    {
        "id": "route-by-topic",
        "name": "Route by topic",
        "description": "Sends money questions to a calculating agent and everything else to cited lookup.",
        "graph": {
            "nodes": [
                _n("start", "start", 40, 160),
                _n(
                    "route",
                    "condition",
                    260,
                    160,
                    "Money question?",
                    branches=[
                        {"label": "money", "expr": "matches(lower(query), '(₹|rs\\.?|amount|percent|%|lakh|crore)')"}
                    ],
                ),
                _n(
                    "calc",
                    "agent",
                    500,
                    80,
                    "Calculate",
                    kind="agentic",
                    tools=["search", "calculator"],
                    prompt="{{ query }}",
                ),
                _n("look", "agent", 500, 240, "Look it up", kind="classic", prompt="{{ query }}"),
                _n("end", "end", 740, 160, output="{{ last }}"),
            ],
            "edges": [
                _e("start", "route"),
                _e("route", "calc", "b0"),
                _e("route", "look", "else"),
                _e("calc", "end"),
                _e("look", "end"),
            ],
        },
    },
]
