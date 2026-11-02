"""The GPU-budget features: env overrides, prompt compression, the guarded
answer cache, extractive-first routing, admission control and the LLM path
against a fake vLLM server."""

from __future__ import annotations

import json
import threading

import httpx
import numpy as np
import pytest

from smriti.ask import AnswerCache, ask
from smriti.config import apply_env_overrides
from smriti.llm import PLATFORM_RULES, Gateway, LLMBusy, build_messages, compress, est_tokens
from smriti.models import Passage


def _p(n: int, text: str, **kw) -> Passage:  # type: ignore[no-untyped-def]
    base = dict(
        n=n,
        chunk_id=f"c{n}",
        doc_id=f"d{n}",
        version=1,
        title=f"Doc {n}",
        ref_no=None,
        doc_date=None,
        doc_type="circular",
        clause_id=None,
        section="",
        page=1,
        text=text,
        score=1.0,
        status="current",
        doc_class="public",
    )
    base.update(kw)
    return Passage(**base)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SMRITI_CHAT_URL", "http://llm:8000/v1")
    monkeypatch.setenv("SMRITI_CHAT_MODEL", "smriti-chat")
    monkeypatch.setenv("SMRITI_CHAT_EXTRA", '{"chat_template_kwargs": {"enable_thinking": false}}')
    monkeypatch.setenv("SMRITI_CHAT_CONCURRENCY", "4")
    monkeypatch.setenv("SMRITI_RERANK_URL", "")  # empty: back to the built-in reranker
    monkeypatch.setenv("SMRITI_CONTEXT_TOKENS", "900")
    out = apply_env_overrides({"models": {"rerank": {"base_url": "http://x", "model": "m"}}})
    chat = out["models"]["chat"]
    assert chat["base_url"] == "http://llm:8000/v1" and chat["max_concurrency"] == 4
    assert chat["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert out["models"]["rerank"] is None
    assert out["generation"]["context_tokens"] == 900


def test_compress_keeps_relevant_sentences_under_budget():
    filler = " ".join(f"Unrelated sentence number {i} about canteen menus." for i in range(60))
    p = _p(1, f"Opening line of the circular. {filler} The performance guarantee is due within 30 days. {filler}")
    out = compress([p], ["performance", "guarantee", "30"], budget_tokens=120)
    assert "within 30 days" in out[0] and out[0].startswith("Opening line")
    assert est_tokens(out[0]) < est_tokens(p.text) / 5


def test_prompt_prefix_is_static_across_users_and_questions():
    a = build_messages("q1", [_p(1, "x")], "policy", "role A", None, None)
    b = build_messages("q2", [_p(2, "y")], "policy", "role B", "skill", "2024-01-01")
    assert a[0] == b[0] and a[0]["content"].startswith(PLATFORM_RULES)  # vLLM prefix cache hits
    assert "role A" in a[-1]["content"] and "role A" not in a[0]["content"]


def test_cache_never_crosses_identifiers():
    c = AnswerCache()
    v = np.ones(4, dtype=np.float32) / 2
    c.put("k", "What does clause 16.1 say?", v, {"answer": "a"})
    assert c.get("k", "what does clause 16.1 say", v, 0.9, 60) == {"answer": "a"}  # normalised exact
    assert c.get("k", "What does clause 16.2 say?", v, 0.9, 60) is None  # same vector, different id
    assert c.get("other-access", "What does clause 16.1 say?", v, 0.9, 60) is None


def test_admission_control_sheds_load():
    from smriti.config import ModelRoute, ModelsConfig

    gw = Gateway(
        ModelsConfig(chat=ModelRoute(base_url="http://x/v1", model="m", max_concurrency=1, queue_timeout_s=0.05))
    )
    r = gw.route("chat")
    with gw.slot(r), pytest.raises(LLMBusy), gw.slot(r):
        pass
    assert gw.usage.shed == 1


class FakeVLLM:
    """Stands in for vLLM's /v1/chat/completions; records what it was sent."""

    def __init__(self, reply: str):
        self.reply = reply
        self.bodies: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self.lock:
            self.bodies.append(body)
        usage = {"prompt_tokens": 500, "completion_tokens": 40}
        return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}], "usage": usage})


def _with_fake_llm(tenant, monkeypatch, reply: str) -> FakeVLLM:  # type: ignore[no-untyped-def]
    from smriti.config import ModelRoute

    fake = FakeVLLM(reply)
    route = ModelRoute(
        base_url="http://llm/v1", model="smriti-chat", extra_body={"chat_template_kwargs": {"enable_thinking": False}}
    )
    monkeypatch.setattr(tenant.gw.cfg, "chat", route)
    client = httpx.Client(base_url="http://llm/v1", transport=httpx.MockTransport(fake))
    monkeypatch.setattr(tenant.gw, "_client", lambda r: client)
    return fake


def test_lookup_is_answered_without_the_gpu(tenant, monkeypatch):
    fake = _with_fake_llm(tenant, monkeypatch, "should not be used [1].")
    r = ask(
        tenant, tenant.principal("asha"), "What is the maximum liquidated damages under clause 46.1?", use_cache=False
    )
    assert fake.bodies == [] and r["model"] == "extractive-v1" and "5%" in r["answer"]
    assert any(e["check"] == "extractive_first" for e in r["guardrails"])


def test_model_answer_is_verified_and_thinking_stripped(tenant, monkeypatch):
    reply = "<think>scratch</think>The waterways of minor bridges and side drains must be cleared before 15 June [1]."
    fake = _with_fake_llm(tenant, monkeypatch, reply)
    r = ask(tenant, tenant.principal("asha"), "What must divisions do before the monsoon?", use_cache=False)
    assert len(fake.bodies) == 1
    sent = fake.bodies[0]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False} and sent["max_tokens"] == 512
    assert "<think>" not in r["answer"] and r["model"] == "smriti-chat" and not r["abstained"]
    assert "Ignore all previous instructions" not in json.dumps(sent) or "WARNING" in json.dumps(sent)


def test_hallucinated_model_answer_is_replaced(tenant, monkeypatch):
    _with_fake_llm(tenant, monkeypatch, "Divisions must stock 9,999 tonnes of cement by 1 January [1].")
    r = ask(tenant, tenant.principal("asha"), "What must divisions do before the monsoon?", use_cache=False)
    assert "9,999" not in r["answer"]
    assert any(e["check"] == "faithfulness" for e in r["guardrails"])
