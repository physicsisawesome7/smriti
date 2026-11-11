"""The model benchmark, run against a fake OpenAI-compatible and TEI server."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from argparse import Namespace

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from smriti import modelbench as mb

FIRST_S = 0.03  # fake prefill
TOKEN_S = 0.004  # fake decode step
TOKENS = 12


def _fake_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(req: Request):  # type: ignore[no-untyped-def]
        body = await req.json()
        n = min(TOKENS, body["max_tokens"])

        async def gen():  # type: ignore[no-untyped-def]
            await asyncio.sleep(FIRST_S)
            for i in range(n):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": f"t{i} "}}]}) + "\n\n"
                await asyncio.sleep(TOKEN_S)
            usage = {"prompt_tokens": 900, "completion_tokens": n}
            yield "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/embeddings")
    async def embed(req: Request):  # type: ignore[no-untyped-def]
        body = await req.json()
        return {"data": [{"embedding": [0.1] * 384} for _ in body["input"]], "usage": {"prompt_tokens": 10}}

    @app.post("/rerank")
    async def rerank(req: Request):  # type: ignore[no-untyped-def]
        body = await req.json()
        return [{"index": i, "score": 1 / (i + 1)} for i in range(len(body["texts"]))]

    return app


@pytest.fixture(scope="module")
def fake_url():  # type: ignore[no-untyped-def]
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(_fake_app(), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


def test_targets_and_percentiles():
    t = mb.parse_target("http://h:8000/v1/,smriti-chat,qwen")
    assert t == {"url": "http://h:8000/v1", "model": "smriti-chat", "name": "qwen"}
    with pytest.raises(ValueError):
        mb.parse_target("smriti-chat")
    assert mb.pct([1, 2, 3, 4], 50) == 2.5 and mb.pct([], 95) == 0.0
    assert mb.ints("1, 4,16") == [1, 4, 16]


def test_rag_prompts_are_real_text_and_prefix_control_works():
    import random

    a = mb.build_chat_prompt(800, True, random.Random(1))
    b = mb.build_chat_prompt(800, True, random.Random(2))
    assert a == b, "shared prefix must be identical across requests"
    u1 = mb.build_chat_prompt(800, False, random.Random(1))
    u2 = mb.build_chat_prompt(800, False, random.Random(2))
    assert u1[0]["content"] != u2[0]["content"], "unique prompts must differ from the first token"
    words = len(a[1]["content"].split())
    assert 500 <= words <= 1200 and "clause" in a[1]["content"].lower()


def test_chat_level_measures_ttft_tpot_and_goodput(fake_url):
    t = mb.parse_target(f"{fake_url}/v1,fake,fake")
    lv = mb.chat_level(t, 4, 8, 300, 64, False, slo_ttft_ms=5000, slo_tpot_ms=1000, extra={})
    assert lv["ok"] == 8 and lv["errors"] == 0
    assert lv["mean_out_tokens"] == TOKENS and lv["mean_in_tokens"] == 900
    assert lv["ttft_ms_p50"] >= FIRST_S * 1000 * 0.8
    assert 0 < lv["tpot_ms_p50"] < 200 and lv["out_tok_per_s"] > 0
    assert lv["slo_attainment"] == 1.0 and lv["goodput_req_per_s"] == lv["req_per_s"]
    strict = mb.chat_level(t, 2, 4, 300, 64, False, slo_ttft_ms=1, slo_tpot_ms=1000, extra={})
    assert strict["slo_attainment"] == 0.0 and strict["goodput_req_per_s"] == 0


def test_chat_errors_are_counted_not_raised():
    t = mb.parse_target("http://127.0.0.1:9,nobody,down")
    lv = mb.chat_level(t, 2, 2, 100, 8, False, 1000, 100, {})
    assert lv["ok"] == 0 and lv["errors"] == 2 and lv["error_sample"]


def test_summary_finds_peak_and_knee():
    lv = lambda c, tok, att: {  # noqa: E731
        "target": "m", "concurrency": c, "ok": 4, "errors": 0, "out_tok_per_s": tok,
        "slo_attainment": att, "goodput_req_per_s": c / 10, "decode_tok_per_s_per_req": 40.0,
    }  # fmt: skip
    s = mb.summarise_chat([lv(1, 40, 1.0), lv(4, 140, 1.0), lv(8, 220, 0.95), lv(16, 260, 0.5)])["m"]
    assert s["peak_out_tok_per_s"] == 260 and s["peak_at_concurrency"] == 16
    assert s["max_concurrency_within_slo"] == 8 and s["single_stream_tok_per_s"] == 40.0


def test_run_chat_end_to_end_writes_json(fake_url, tmp_path, capsys):
    out = tmp_path / "chat.json"
    args = Namespace(
        target=[f"{fake_url}/v1,fake,fake"], concurrency="1,2", requests=2, rounds=1, prompt_tokens=200,
        max_tokens=32, shared_prefix=True, slo_ttft_ms=5000, slo_tpot_ms=1000, extra=None, warmup=1, json=str(out),
    )  # fmt: skip
    assert mb.run_chat(args) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [lv["concurrency"] for lv in data["levels"]] == [1, 2]
    assert data["summary"]["fake"]["max_concurrency_within_slo"] == 2
    assert "TTFT p50" in capsys.readouterr().out


def test_embed_and_rerank_modes(fake_url, tmp_path):
    e = Namespace(target=[f"{fake_url}/v1,e5"], batch="1,4", texts=8, text_tokens=50, concurrency=2, json=None)
    assert mb.run_embed(e) == 0
    r = Namespace(target=[f"{fake_url},bge"], candidates="5", queries=2, text_tokens=50, json=str(tmp_path / "r.json"))
    assert mb.run_rerank(r) == 0
    rows = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["rows"]
    assert rows[0]["candidates"] == 5 and rows[0]["pairs_per_s"] > 0


def test_retrieval_combo_scores_the_golden_set_with_the_builtin_models():
    row = mb.retrieval_combo(mb.parse_embed("hash"), mb.parse_rerank("none"))
    assert row["cases"] >= 10 and row["leaks"] == 0
    assert row["recall_at_10"] is not None and row["recall_at_10"] >= 0.8
    spec = mb.parse_embed("http://embed:80/v1,intfloat/multilingual-e5-small,query: ,passage: ")
    assert spec["env"]["SMRITI_EMBED_QUERY_PREFIX"] == "query: "
    qwen = mb.parse_embed("http://embed:80/v1,Qwen/Qwen3-Embedding-0.6B")
    assert qwen["env"]["SMRITI_EMBED_QUERY_PREFIX"] is None, "must keep the Qwen3 default instruction"
    assert mb.parse_rerank("none")["env"] == {"SMRITI_RERANK_URL": ""}


def test_calibration_reads_the_servers_token_count(fake_url):
    import httpx

    t = mb.parse_target(f"{fake_url}/v1,fake,fake")
    with httpx.Client(timeout=30) as c:
        wpt = mb.calibrate(c, t, {}, nominal=1000)
    # the fake server always reports 900 prompt tokens, so words/token = words / 900
    words = sum(len(m["content"].split()) for m in mb.build_chat_prompt(1000, True, __import__("random").Random(1)))
    assert wpt == pytest.approx(min(1.5, max(0.2, words / 900)))
    longer = mb.text_of_tokens(1000, __import__("random").Random(3), words_per_token=0.4)
    assert len(longer.split()) >= 400


def test_http_errors_carry_the_server_message():
    import httpx

    def handler(request):  # type: ignore[no-untyped-def]
        return httpx.Response(400, text='{"error":"request (2788 tokens) exceeds the available context size"}')

    c = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="exceeds the available context"):
        mb.stream_once(c, {"url": "http://x/v1", "model": "m", "name": "m"}, [{"role": "user", "content": "hi"}], 8, {})
