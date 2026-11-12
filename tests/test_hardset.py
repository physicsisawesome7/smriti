"""The hard set: generation, sliced scoring, comparison and drafting candidates."""

from __future__ import annotations

import pytest
import yaml

from smriti import evals
from smriti.config import load_pack
from smriti.hardset import SLICES, write_pack


def test_pack_is_reproducible_and_loads(tmp_path):  # type: ignore[no-untyped-def]
    a = write_pack(tmp_path / "a")
    b = write_pack(tmp_path / "b")
    assert a["documents"] == b["documents"] >= 250 and a["questions"] >= 150
    assert all(a["slices"][s] >= 12 for s in SLICES)
    ga = (tmp_path / "a" / "evals" / "golden.yaml").read_text(encoding="utf-8")
    assert ga == (tmp_path / "b" / "evals" / "golden.yaml").read_text(encoding="utf-8")
    assert write_pack(tmp_path / "c", seed=8)["documents"] == a["documents"]
    assert (tmp_path / "c" / "evals" / "golden.yaml").read_text(encoding="utf-8") != ga  # another seed, other facts
    pack = load_pack(tmp_path / "a")
    cases = pack.evals["golden"]
    assert len(cases) == a["questions"] and len({c["id"] for c in cases}) == len(cases)
    # every expected or forbidden document exists in the corpus
    refs = {
        yaml.safe_load(p.read_text(encoding="utf-8").split("---")[1])["ref_no"]
        for p in (tmp_path / "a" / "corpus").glob("*.md")
    }
    for c in cases:
        assert set(c.get("expect_docs", [])) | set(c.get("forbidden_docs", [])) <= refs, c["id"]


@pytest.fixture(scope="module")
def baseline():  # type: ignore[no-untyped-def]
    return evals.run_hard(evals.parse_config("hash:SMRITI_CHAT_URL=,SMRITI_EMBED_URL=,SMRITI_RERANK_URL="))


def test_hard_set_separates_what_works_from_what_does_not(baseline):  # type: ignore[no-untyped-def]
    s = baseline["slices"]
    assert set(s) == set(SLICES) and baseline["leaks"] == 0
    # with no neural model: lookups by identifier and amendment chains work, permissions hold...
    assert s["exact"]["pass_rate"] >= 0.9 and s["superseded"]["pass_rate"] >= 0.9
    assert s["restricted"]["pass_rate"] == 1.0
    # ...and the set is not saturated: paraphrase and cross-language need a real embedder
    assert baseline["passed"] / baseline["cases"] < 0.8
    assert s["paraphrase"]["pass_rate"] < 0.5 and s["cross-language"]["pass_rate"] < 0.5
    assert "slice" in evals.format_hard(baseline)
    assert evals.hard_gate(baseline) == []
    broken = {**baseline, "leaks": 2, "slices": {**s, "exact": {**s["exact"], "pass_rate": 0.5}}}
    assert len(evals.hard_gate(broken)) == 2


def test_compare_is_paired_and_marks_significance(baseline):  # type: ignore[no-untyped-def]
    same = evals.compare(baseline, baseline)
    assert same["overall"]["diff"] == 0 and not same["overall"]["significant"]
    better = {
        **baseline,
        "config": "better",
        "rows": [{**r, "pass": True} if r["slice"] == "paraphrase" else r for r in baseline["rows"]],
    }
    c = evals.compare(baseline, better)
    assert c["slices"]["paraphrase"]["diff"] > 0.5 and c["slices"]["paraphrase"]["significant"]
    assert c["slices"]["exact"]["diff"] == 0 and not c["slices"]["exact"]["lost"]
    assert c["overall"]["significant"] and len(c["overall"]["gained"]) == len(c["slices"]["paraphrase"]["gained"])
    assert c["retrieved"]["overall"]["diff"] == 0  # only the pass flag was changed
    assert "*" in evals.format_compare(c) and "retrieved" in evals.format_compare(c)


def test_parse_config():
    assert evals.parse_config("base") == {"name": "base", "env": {}}
    c = evals.parse_config("e5:SMRITI_EMBED_URL=http://h/v1, SMRITI_RERANK_URL=")
    assert c == {"name": "e5", "env": {"SMRITI_EMBED_URL": "http://h/v1", "SMRITI_RERANK_URL": ""}}
    with pytest.raises(ValueError):
        evals.parse_config("x:novalue")


def test_build_candidates_from_the_tenants_own_documents(tenant, monkeypatch):  # type: ignore[no-untyped-def]
    out = evals.build_candidates(tenant, "asha", count=5)
    assert out["drafted_by"] == "rules" and len(out["cases"]) == 5
    for c in out["cases"]:
        assert c["expect_text"][0] in c["evidence"] and "____" in c["question"] and c["as_user"] == "asha"
    # the bidder may read only published rules, so nothing is drafted from anything else
    visible = {
        d.get("ref_no") or d["doc_id"]
        for d in tenant.store.list_documents(tenant.id)
        if d["access"].get("doc_class") == "public"
    }
    assert {c["expect_docs"][0] for c in evals.build_candidates(tenant, "bidder", count=50)["cases"]} <= visible

    # with a model: the answer must be copied from the passage and not given away in the question
    drafts = iter(
        [
            {"question": "How long is the window?", "answer": "not in the passage at all"},
            {"question": "Is it 21 days?", "answer": "21 days"},
        ]
    )
    monkeypatch.setattr(tenant.gw, "available", lambda task="chat": True)
    monkeypatch.setattr(tenant.gw, "extract_json", lambda *a, **k: next(drafts, None))
    monkeypatch.setattr(evals, "ask", lambda *a, **k: {"passages": [], "answer": "", "abstained": True})
    out = evals.build_candidates(tenant, "asha", count=3)
    assert out["drafted_by"] == "model" and out["rejected_drafts"] >= 1
    assert all(c["passes_now"] is False for c in out["cases"])
