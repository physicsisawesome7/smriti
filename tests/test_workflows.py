"""Workflows: graph validation, the expression language, running, approvals and limits."""

from __future__ import annotations

import copy
import json
import re

import pytest
from fastapi.testclient import TestClient
from test_agents import LD, PG, _fake_llm, _rest_tool, quote_first_passage

from smriti import agents
from smriti.agents import runtime as rt
from smriti.agents import workflow as wf
from smriti.agents.expr import ExprError, evaluate, render


def _template(tid):  # type: ignore[no-untyped-def]
    return copy.deepcopy(next(t for t in wf.TEMPLATES if t["id"] == tid)["graph"])


def _workflow(tenant, user, graph, **kw):  # type: ignore[no-untyped-def]
    body = {"name": kw.pop("name", "Test workflow"), "kind": "workflow", "graph": graph, **kw}
    return agents.save(tenant, tenant.principal(user), body)


def _go(tenant, platform, user, agent, q):  # type: ignore[no-untyped-def]
    return list(rt.chat(tenant, tenant.principal(user), platform.box, agent, q))


# ---------------------------------------------------------------- expressions


def test_expressions_are_safe_and_useful():  # type: ignore[no-untyped-def]
    v = {"answer": "Clause 16.1 [1]", "n": "4,80,000", "items": [{"a": 1}, {"a": 2}]}
    assert evaluate("len(answer) > 0 and contains(answer, 'clause')", v) is True
    assert evaluate("float(n) * 0.05", v) == 24000
    assert evaluate("pluck(items, 'a')", v) == [1, 2]
    assert evaluate("[strip(x) for x in split('a ; b', ';')]", v) == ["a", "b"]
    assert evaluate("'yes' if missing else 'no'", v) == "no"
    assert render("Total: {{ float(n) + 20000 }}", v) == "Total: 500000"
    for bad in (
        "__import__('os')",
        "answer.__class__",
        "(lambda: 1)()",
        "open('x')",
        "[x for x in items for y in items]",
        "9 ** 999",
    ):
        with pytest.raises(ExprError):
            evaluate(bad, v)


# ---------------------------------------------------------------- validation


def test_invalid_graphs_are_rejected_with_reasons(tenant):  # type: ignore[no-untyped-def]
    good = _template("answer-approve")
    cases = {
        "exactly one Start": lambda g: g["nodes"].pop(0),
        "not connected": lambda g: g["edges"].remove(next(e for e in g["edges"] if e["handle"] == "else")),
        "not reachable": lambda g: g["nodes"].append({"id": "orphan", "type": "end", "data": {}}),
        "nothing can connect into": lambda g: g["edges"].append(
            {"source": "ok", "target": "start", "handle": "approved"}
        ),
        "not allowed": lambda g: g["nodes"][2]["data"]["branches"][0].update(expr="draft.__len__()"),
        "not a valid variable": lambda g: g["nodes"][1]["data"].update(output="1bad"),
        "unknown tool": lambda g: g["nodes"][1]["data"].update(tools=["shell"]),
    }
    for reason, breaker in cases.items():
        g = copy.deepcopy(good)
        breaker(g)
        with pytest.raises(agents.AgentError, match=reason):
            _workflow(tenant, "asha", g)
    saved = _workflow(tenant, "asha", good)
    assert saved["kind"] == "workflow" and len(saved["graph"]["nodes"]) == 7


# ---------------------------------------------------------------- running


def test_approval_node_pauses_and_routes(tenant, platform):  # type: ignore[no-untyped-def]
    a = _workflow(tenant, "asha", _template("answer-approve"), name="Sign-off flow")
    events = _go(tenant, platform, "asha", a, PG)
    assert events[-1]["event"] == "approval"
    ap = events[-1]["data"]
    assert ap["action"] == "Sign-off" and "Release this answer?" in ap["description"]
    assert [s["node"] for s in (e["data"] for e in events if e["event"] == "step") if s.get("type") == "node"][:3] == [
        "start",
        "draft",
        "found",
    ]
    p = tenant.principal("asha")
    with pytest.raises(PermissionError):
        list(rt.approve(tenant, tenant.principal("vikram"), platform.box, ap["run_id"], True))
    r = rt.run_sync(rt.approve(tenant, p, platform.box, ap["run_id"], True))
    assert r["status"] == "done" and not r["abstained"] and r["citations"]
    assert re.search(r"\[\d+\]", r["answer"]) and r["agent"]["kind"] == "workflow"
    with pytest.raises(ValueError):  # used once
        list(rt.approve(tenant, p, platform.box, ap["run_id"], True))

    ap2 = _go(tenant, platform, "asha", a, PG)[-1]["data"]
    r2 = rt.run_sync(rt.approve(tenant, p, platform.box, ap2["run_id"], False))
    assert r2["answer"] == "A reviewer held this answer back."


def test_no_answer_takes_the_else_branch(tenant, platform):  # type: ignore[no-untyped-def]
    a = _workflow(
        tenant,
        "asha",
        _template("answer-approve"),
        name="Empty sources flow",
        sources={"mode": "selected", "doc_types": ["no-such-type"]},
    )
    r = rt.run_sync(_go_iter(tenant, platform, "asha", a, PG))
    assert r["answer"].startswith("No supported answer was found")


def _go_iter(tenant, platform, user, agent, q):  # type: ignore[no-untyped-def]
    return rt.chat(tenant, tenant.principal(user), platform.box, agent, q)


def test_loop_answers_each_question_with_global_citations(tenant, platform):  # type: ignore[no-untyped-def]
    a = _workflow(tenant, "asha", _template("loop-questions"), name="Batch questions")
    r = rt.run_sync(_go_iter(tenant, platform, "asha", a, f"{PG}; {LD}"))
    assert r["status"] == "done" and len(r["citations"]) >= 2
    low = r["answer"].lower()
    assert "performance guarantee" in low and "liquidated damages" in low
    nums = {int(n) for n in re.findall(r"\[(\d+)\]", r["answer"])}
    assert nums <= {c["n"] for c in r["passages"]}  # renumbered into one list


def test_condition_routes_money_questions_to_the_calculator(tenant, platform):  # type: ignore[no-untyped-def]
    a = _workflow(tenant, "asha", _template("route-by-topic"), name="Router")
    events = _go(tenant, platform, "asha", a, "What is 5% of 4,80,00,000?")
    steps = [e["data"] for e in events if e["event"] == "step"]
    assert any(s.get("node") == "route" and "money" in s.get("summary", "") for s in steps)
    assert any(s.get("node") == "calc" for s in steps)
    assert "24,00,000" in events[-1]["data"]["answer"] or "2400000" in events[-1]["data"]["answer"]


def test_cycles_stop_at_the_step_limit(tenant, platform):  # type: ignore[no-untyped-def]
    g = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "inc", "type": "set_state", "data": {"assign": [{"name": "n", "expr": "default(n, 0) + 1"}]}},
            {"id": "c", "type": "condition", "data": {"branches": [{"expr": "n < 1000"}]}},
            {"id": "e", "type": "end", "data": {"output": "{{ n }}"}},
        ],
        "edges": [
            {"source": "s", "target": "inc"},
            {"source": "inc", "target": "c"},
            {"source": "c", "target": "inc", "handle": "b0"},
            {"source": "c", "target": "e", "handle": "else"},
        ],
    }
    r = rt.run_sync(_go_iter(tenant, platform, "asha", _workflow(tenant, "asha", g, name="Cycle"), "go"))
    assert r["abstained"] and f"{wf.MAX_STEPS} steps" in r["answer"]


def test_expression_errors_stop_the_run_cleanly(tenant, platform):  # type: ignore[no-untyped-def]
    g = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "x", "type": "set_state", "title": "Divide", "data": {"assign": [{"name": "y", "expr": "1 / 0"}]}},
            {"id": "e", "type": "end"},
        ],
        "edges": [{"source": "s", "target": "x"}, {"source": "x", "target": "e"}],
    }
    r = rt.run_sync(_go_iter(tenant, platform, "asha", _workflow(tenant, "asha", g, name="Div"), "go"))
    assert r["abstained"] and "'Divide' failed: division by zero" in r["answer"]


def test_code_node_reads_state_in_the_sandbox(tenant, platform, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SMRITI_SANDBOX", "local")
    g = {
        "nodes": [
            {"id": "s", "type": "start"},
            {
                "id": "py",
                "type": "code",
                "data": {"code": "print(json.dumps({'words': len(state['query'].split())}))", "output": "stats"},
            },
            {"id": "e", "type": "end", "data": {"output": "{{ stats['words'] }} words"}},
        ],
        "edges": [{"source": "s", "target": "py"}, {"source": "py", "target": "e"}],
    }
    r = rt.run_sync(_go_iter(tenant, platform, "asha", _workflow(tenant, "asha", g, name="Code"), "one two three"))
    assert r["answer"] == "3 words"
    monkeypatch.setenv("SMRITI_SANDBOX", "off")
    r2 = rt.run_sync(_go_iter(tenant, platform, "asha", _workflow(tenant, "asha", g, name="Code off"), "x"))
    assert r2["abstained"] and "code execution is off" in r2["answer"]


def test_tool_approval_inside_an_agent_step_pauses_the_workflow(tenant, platform, monkeypatch):  # type: ignore[no-untyped-def]
    log: list = []
    tool = _rest_tool(tenant, platform, monkeypatch, log)
    fn = f"t{tool['id']}_create_ticket"
    _fake_llm(tenant, monkeypatch, [lambda b: {"calls": [(fn, {"title": "Signal fault"})]}, quote_first_passage])
    g = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "act", "type": "agent", "data": {"kind": "agentic", "tools": ["search", tool["ref"]]}},
            {"id": "e", "type": "end", "data": {"output": "Done: {{ answer }}"}},
        ],
        "edges": [{"source": "s", "target": "act"}, {"source": "act", "target": "e"}],
    }
    a = _workflow(tenant, "asha", g, name="Ticket flow")
    assert tool["ref"] in a["tools"]
    events = _go(tenant, platform, "asha", a, "Open a ticket for the signal fault")
    ap = events[-1]["data"]
    assert events[-1]["event"] == "approval" and ap["action"] == "create_ticket" and not log
    assert ap["run_id"] == events[0]["data"]["run_id"]  # approving resumes the workflow, not just the step
    r = rt.run_sync(rt.approve(tenant, tenant.principal("asha"), platform.box, ap["run_id"], True))
    assert [m for m, _, _ in log] == ["POST"] and r["answer"].startswith("Done:")


# ---------------------------------------------------------------- API


def test_catalog_and_preview_over_http(fresh_app):  # type: ignore[no-untyped-def]
    c = TestClient(fresh_app)
    h = {"X-Smriti-User": "asha"}
    cat = c.get("/api/agents/catalog", headers=h).json()
    assert "workflow" in {k["kind"] for k in cat["kinds"]}
    assert {t["id"] for t in cat["workflow"]["templates"]} >= {"answer-approve", "loop-questions", "route-by-topic"}
    bad = c.post("/api/agents", json={"name": "x", "kind": "workflow", "graph": {"nodes": [], "edges": []}}, headers=h)
    assert bad.status_code == 400 and "Start" in bad.text
    body = {
        "agent": {"name": "Try", "kind": "workflow", "graph": _template("loop-questions")},
        "question": f"{PG}; {LD}",
    }
    with c.stream("POST", "/api/agents/preview", json=body, headers=h) as r:
        text = "".join(r.iter_text())
    final = json.loads(text.rsplit("data: ", 1)[1])
    assert final["agent"]["kind"] == "workflow" and final["status"] == "done" and final["citations"]
