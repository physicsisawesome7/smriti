"""The agent inventory and the built-in skills every tenant gets."""

from __future__ import annotations

from fastapi.testclient import TestClient

from smriti import agents, skills
from smriti.agents import inventory

BUILTIN = {"pii-scan", "compare-documents", "deadline-tracker", "rti-reply", "citation-check"}


def _run(tenant, user, name, inputs):  # type: ignore[no-untyped-def]
    return skills.run_skill(tenant, tenant.principal(user), name, inputs)


def _checks(res, severity):  # type: ignore[no-untyped-def]
    return {f["check"] for f in res["findings"] if f["severity"] == severity}


# ---------------------------------------------------------------- inventory


def test_every_inventory_template_saves_as_a_valid_agent(tenant):
    km = tenant.principal("km")
    ids = [t["id"] for t in inventory.TEMPLATES]
    assert len(ids) == len(set(ids)) >= 15
    for t in inventory.templates():
        body = {k: v for k, v in t.items() if k not in ("id", "category", "category_label")}
        saved = agents.save(tenant, km, {**body, "name": f"inv {t['name']}"})
        assert saved["kind"] == t["kind"], t["id"]
        assert t["category"] in inventory.CATEGORIES


def test_inventory_uses_only_offline_capable_builtin_tools():
    from smriti.agents.tools import BUILTIN as TOOLS

    for t in inventory.TEMPLATES:
        assert set(t["tools"]) <= set(TOOLS), t["id"]
        assert not any(r.startswith("tool:") for r in t["tools"]), t["id"]


def test_inventory_skill_hints_name_real_skills(tenant):
    for t in inventory.TEMPLATES:
        if "skills" in t["tools"]:
            named = {s for s in tenant.pack.skills if s in t["instructions"]}
            assert named or "skill" not in t["instructions"], t["id"]


def test_catalog_serves_the_inventory(fresh_app):
    c = TestClient(fresh_app, headers={"X-Smriti-User": "asha"})
    cat = c.get("/api/agents/catalog").json()
    assert cat["categories"] == inventory.CATEGORIES
    assert {t["id"] for t in cat["agent_templates"]} == {t["id"] for t in inventory.TEMPLATES}
    assert all(t["category_label"] for t in cat["agent_templates"])


# ---------------------------------------------------------------- built-in skills


def test_builtin_skills_load_for_every_tenant(tenant):
    assert set(tenant.pack.skills) >= BUILTIN
    # pack skills still load alongside them
    assert {"validate-tender", "circular-impact"} <= set(tenant.pack.skills)
    listed = {s["name"]: s for s in skills.list_skills(tenant, tenant.principal("bidder"))}
    assert listed["citation-check"]["allowed"] and not listed["rti-reply"]["allowed"]


def test_pii_scan_counts_without_showing_values(tenant):
    res = _run(tenant, "rakesh", "pii-scan", {"document": "LA/P-17/SCH/2025"})
    assert res["fields"]["categories"]["aadhaar"] > 0
    text = str(res)
    assert not __import__("re").search(r"\b\d{4}\s?\d{4}\s?\d{4}\b", text), "an Aadhaar number leaked"
    clean = _run(tenant, "asha", "pii-scan", {"document": "GCC"})
    assert _checks(clean, "pass") == {"pii_found"}


def test_skills_cannot_open_what_the_person_cannot(tenant):
    secret = _run(tenant, "bidder", "pii-scan", {"document": "VIG/2025/12"})
    missing = _run(tenant, "bidder", "pii-scan", {"document": "NO/SUCH/DOC"})
    assert _checks(secret, "major") == _checks(missing, "major") == {"document_found"}
    assert secret["findings"][0]["message"].replace("VIG/2025/12", "X") == missing["findings"][0]["message"].replace(
        "NO/SUCH/DOC", "X"
    )


def test_compare_documents_redlines_the_changed_figures(tenant):
    res = _run(tenant, "vikram", "compare-documents", {"old": "Powers of acceptance 2019", "new": "POA 2024"})
    assert res["fields"]["changed"] >= 3
    assert any("[-25-] {+50+}" in c["redline"] for c in res["extra"]["changes"])


def test_deadline_tracker_finds_periods_and_due_dates(tenant):
    res = _run(tenant, "asha", "deadline-tracker", {"document": "GCC", "start_date": "2025-01-01"})
    rows = {r["clause"]: r for r in res["extra"]["rows"]}
    assert rows["16.1"]["period"] == "21 days" and rows["16.1"]["due"] == "2025-01-22"
    assert res["extra"]["csv"].startswith("kind,due,period,clause,sentence")


def test_rti_reply_checks_deadline_exemptions_and_never_quotes_injected_text(tenant):
    res = _run(
        tenant,
        "vikram",
        "rti-reply",
        {
            "received_date": "2025-05-01",
            "as_of": "2025-06-15",
            "questions": "1. What is the period for submitting the performance guarantee?\n"
            "2. Copy of the vigilance report on case 12\n3. Number of elephants in the zoo",
        },
    )
    assert "deadline" in _checks(res, "critical")
    assert "exemption_8(1)(h)" in _checks(res, "major")
    draft = res["draft"]["content"]
    assert draft.startswith(skills.DRAFT_MARK)
    assert "Ignore all previous instructions" not in draft
    assert draft.count("not held by this public authority") == 2
    assert any("CPIO" in f for f in res["draft"]["open_fields"])


def test_rti_reply_needs_the_draft_grant(tenant):
    import pytest

    with pytest.raises(PermissionError):
        _run(tenant, "bidder", "rti-reply", {"received_date": "2025-05-01", "questions": "anything at all"})


def test_citation_check_catches_wrong_numbers_and_reversed_rules(tenant):
    res = _run(
        tenant,
        "asha",
        "citation-check",
        {
            "text": "The performance guarantee must be submitted within 30 days. "
            "The maximum liquidated damages is 25% of the contract value. "
            "Bidders shall not be required to pay EMD."
        },
    )
    f = res["fields"]
    assert f["supported"] == 1 and f["number_mismatch"] == 1 and f["negation_mismatch"] == 1
