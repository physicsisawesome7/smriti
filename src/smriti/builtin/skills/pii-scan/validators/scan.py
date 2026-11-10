"""Personal-data scan: counts by category, checked against the access record."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from smriti.privacy import mask_text
from smriti.skilllib import doc_citation, not_found, open_document, raw_text

TOKEN = re.compile(r"\[([A-Z][A-Z_]+)\]")


def run(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    ref = str(inputs.get("document") or "")
    opened = open_document(ctx, ref)
    if not opened:
        return {"findings": [not_found(ref)]}
    d, _ = opened
    text = raw_text(ctx, d)
    masked, cats = mask_text(text)
    counts = Counter(m.lower() for m in TOKEN.findall(masked)) - Counter(m.lower() for m in TOKEN.findall(text))
    cite = doc_citation(d)
    access = d["access"]
    declared = {c.lower() for c in access.get("pii") or []}
    findings: list[dict[str, Any]] = []
    if not cats:
        findings.append(
            {"check": "pii_found", "severity": "pass", "message": "No personal data detected.", "citation": cite}
        )
    else:
        summary = ", ".join(f"{n} × {c}" for c, n in sorted(counts.items()))
        if access.get("doc_class") == "public":
            findings.append(
                {
                    "check": "public_with_pii",
                    "severity": "critical",
                    "message": f"Document is public but holds personal data ({summary}). Reclassify it or redact before publishing.",
                    "citation": cite,
                }
            )
        undeclared = sorted(c for c in cats if c not in declared)
        if undeclared:
            findings.append(
                {
                    "check": "masking_not_configured",
                    "severity": "major",
                    "message": f"Personal data not listed in the access record, so it is not masked by role: {', '.join(undeclared)}.",
                    "citation": cite,
                }
            )
        findings.append(
            {"check": "pii_found", "severity": "info", "message": f"Detected: {summary}.", "citation": cite}
        )
    ctx.citations.append(cite)
    return {
        "fields": {"document": d["title"], "doc_class": access.get("doc_class"), "categories": dict(counts)},
        "findings": findings,
    }
