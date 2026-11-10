"""Paragraph-level comparison with word-level redlines."""

from __future__ import annotations

import difflib
import re
from typing import Any

from smriti.skilllib import doc_citation, not_found, open_document

MAX_CHANGES = 40


def paragraphs(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", p).strip()
        for p in re.split(r"\n\s*\n|\n(?=\s*[-*#|]|\s*\d+(?:\.\d+)*\s)", text)
        if p.strip()
    ]


def redline(old: str, new: str) -> str:
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(" ".join(a[i1:i2]))
        if op in ("delete", "replace"):
            out.append("[-" + " ".join(a[i1:i2]) + "-]")
        if op in ("insert", "replace"):
            out.append("{+" + " ".join(b[j1:j2]) + "+}")
    return " ".join(out)


def run(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    refs = {k: str(inputs.get(k) or "") for k in ("old", "new")}
    opened = {k: open_document(ctx, v) for k, v in refs.items()}
    missing = [not_found(refs[k], f"{k}_found") for k, v in opened.items() if not v]
    if missing:
        return {"findings": missing}
    (d_old, t_old), (d_new, t_new) = opened["old"], opened["new"]  # type: ignore[misc]
    a, b = paragraphs(t_old), paragraphs(t_new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    changes: list[dict[str, Any]] = []
    added = removed = changed = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op == "replace":
            for k in range(max(i2 - i1, j2 - j1)):
                o = a[i1 + k] if i1 + k < i2 else ""
                n = b[j1 + k] if j1 + k < j2 else ""
                kind = "changed" if o and n else "removed" if o else "added"
                changes.append({"kind": kind, "old": o, "new": n, "redline": redline(o, n) if o and n else (n or o)})
        elif op == "delete":
            changes.extend({"kind": "removed", "old": p, "new": "", "redline": p} for p in a[i1:i2])
        else:
            changes.extend({"kind": "added", "old": "", "new": p, "redline": p} for p in b[j1:j2])
    for c in changes:
        added += c["kind"] == "added"
        removed += c["kind"] == "removed"
        changed += c["kind"] == "changed"
    ratio = difflib.SequenceMatcher(None, t_old, t_new, autojunk=False).ratio()
    c_old, c_new = doc_citation(d_old), doc_citation(d_new)
    ctx.citations.extend([c_old, c_new])
    findings = [
        {
            "check": f"paragraph_{c['kind']}",
            "severity": "info",
            "message": c["redline"][:600],
            "citation": c_new if c["kind"] != "removed" else c_old,
        }
        for c in changes[:MAX_CHANGES]
    ]
    if not changes:
        findings.append(
            {"check": "identical", "severity": "pass", "message": "No differences in the text.", "citation": c_new}
        )
    elif len(changes) > MAX_CHANGES:
        findings.append(
            {
                "check": "more_changes",
                "severity": "info",
                "message": f"{len(changes) - MAX_CHANGES} more changes not listed.",
                "citation": None,
            }
        )
    return {
        "fields": {
            "old": d_old["title"],
            "new": d_new["title"],
            "similarity": round(ratio, 3),
            "added": added,
            "removed": removed,
            "changed": changed,
        },
        "findings": findings,
        "extra": {"changes": changes},
    }
