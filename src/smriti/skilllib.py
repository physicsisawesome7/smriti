"""Helpers shared by skill validators.

Validators load by file path, so they import these from ``smriti.skilllib``.
Everything here goes through the same access rules as chat: a document is
opened only when the person running the skill may read it, and its text comes
back masked for their role.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .access import evaluate


def not_found(ref: str, check: str = "document_found") -> dict[str, Any]:
    return {
        "check": check,
        "severity": "major",
        "message": f"No document you can open matches '{ref}'.",
        "citation": None,
    }


def open_document(ctx: Any, ref: str) -> tuple[dict[str, Any], str] | None:
    """A document by id, reference number or alias, with its text rendered for
    the person running the skill (PII masked by role). None when there is no
    such document or they may not open it: the two look the same on purpose."""
    t, p = ctx.tenant, ctx.principal
    ref = (ref or "").strip()
    if not ref:
        return None
    d = t.store.get_document(t.id, ref) or t.store.find_by_ref(t.id, ref)
    if not d or not evaluate(p, d["access"], d["doc_type"]).allowed:
        return None
    parts = [t.render_for(p, c, d)[0] for c in t.store.doc_chunks(t.id, d["doc_id"])]
    return d, "\n\n".join(parts)


def raw_text(ctx: Any, d: dict[str, Any]) -> str:
    """Unmasked text, for checks that only count personal data and never show it."""
    return "\n\n".join(c["text"] for c in ctx.tenant.store.doc_chunks(ctx.tenant.id, d["doc_id"]))


def doc_citation(d: dict[str, Any], clause: str | None = None) -> dict[str, Any]:
    return {
        "doc_id": d["doc_id"],
        "title": d.get("title"),
        "ref_no": d.get("ref_no"),
        "clause_id": clause,
        "section": None,
        "date": d.get("doc_date"),
    }


MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}

DATE_PATTERNS = (
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"),
    re.compile(r"\b(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{4})\b"),
    re.compile(r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[A-Za-z]{3,9})\.?,?\s+(?P<y>\d{4})\b"),
    re.compile(r"\b(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})\b"),
)


def find_dates(text: str) -> list[tuple[int, int, dt.date]]:
    """Every calendar date in the text as (start, end, date). Indian order
    (day first) for numeric dates, as written in government documents."""
    out: list[tuple[int, int, dt.date]] = []
    taken: list[tuple[int, int]] = []
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue
            g = m.groupdict()
            month = int(g["m"]) if g.get("m") else MONTHS.get((g.get("mon") or "").lower())
            if not month:
                continue
            try:
                day = dt.date(int(g["y"]), month, int(g["d"]))
            except ValueError:
                continue
            out.append((m.start(), m.end(), day))
            taken.append((m.start(), m.end()))
    return sorted(out, key=lambda x: x[0])


def parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    s = s.strip()
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        found = find_dates(s)
        return found[0][2] if found else None
