"""Deadlines and dated obligations, each with its source sentence."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from smriti.index import split_sentences
from smriti.skilllib import doc_citation, find_dates, not_found, open_document, parse_date

PERIOD = re.compile(
    r"\b(?:within|not later than|no later than|before the expiry of|in|after)\s+(?:a period of\s+)?"
    r"(?P<n>\d{1,4}|one|two|three|four|five|six|seven|ten|fourteen|fifteen|thirty|sixty|ninety)\s*"
    r"(?:\(\w+\)\s*)?(?P<unit>working days?|days?|weeks?|months?|years?)\b",
    re.I,
)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "fourteen": 14,
         "fifteen": 15, "thirty": 30, "sixty": 60, "ninety": 90}  # fmt: skip
CLAUSE = re.compile(r"^\s*(?:clause\s+)?(\d+(?:\.\d+)*)\b", re.I)
OBLIGATION = re.compile(r"\b(shall|must|is required to|will be|should|due|last date|closing|deadline|by)\b", re.I)


def _add(start: dt.date, n: int, unit: str) -> dt.date:
    unit = unit.lower()
    if unit.startswith("working"):
        d, left = start, n
        while left:
            d += dt.timedelta(days=1)
            if d.weekday() < 5:
                left -= 1
        return d
    if unit.startswith("day"):
        return start + dt.timedelta(days=n)
    if unit.startswith("week"):
        return start + dt.timedelta(weeks=n)
    months = n * (12 if unit.startswith("year") else 1)
    y, m = divmod(start.month - 1 + months, 12)
    day = min(start.day, 28)
    return dt.date(start.year + y, m + 1, day)


def run(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    ref = str(inputs.get("document") or "")
    opened = open_document(ctx, ref)
    if not opened:
        return {"findings": [not_found(ref)]}
    d, text = opened
    start = parse_date(inputs.get("start_date"))
    cite = doc_citation(d)
    ctx.citations.append(cite)
    rows: list[dict[str, Any]] = []
    clause = None
    for para in re.split(r"\n\s*\n", text):
        if m := CLAUSE.match(para):
            clause = m.group(1)
        for s in split_sentences(para):
            sent = re.sub(r"\s+", " ", s).strip()
            if m := CLAUSE.match(sent):
                clause = m.group(1)
            for _, _, day in find_dates(sent):
                rows.append(
                    {"kind": "date", "due": day.isoformat(), "period": None, "clause": clause, "sentence": sent}
                )
            for m in PERIOD.finditer(sent):
                n_raw = m.group("n").lower()
                n = int(n_raw) if n_raw.isdigit() else WORDS[n_raw]
                period = f"{n} {m.group('unit').lower()}"
                due = _add(start, n, m.group("unit")).isoformat() if start else None
                rows.append({"kind": "period", "due": due, "period": period, "clause": clause, "sentence": sent})
    findings: list[dict[str, Any]] = []
    for r in rows:
        label = (
            r["due"] if r["kind"] == "date" else f"within {r['period']}" + (f" (due {r['due']})" if r["due"] else "")
        )
        where = f"Clause {r['clause']}: " if r["clause"] else ""
        sev = "info" if OBLIGATION.search(r["sentence"]) else "minor"
        findings.append({"check": "deadline", "severity": sev, "message": f"{label}. {where}{r['sentence'][:300]}",
                         "citation": {**cite, "clause_id": r["clause"]}})  # fmt: skip
    if not rows:
        findings.append(
            {"check": "deadline", "severity": "pass", "message": "No dates or periods found.", "citation": cite}
        )
    csv_lines = ["kind,due,period,clause,sentence"] + [
        ",".join(
            '"' + str(r[k] or "").replace('"', '""') + '"' for k in ("kind", "due", "period", "clause", "sentence")
        )
        for r in rows
    ]
    return {
        "fields": {"document": d["title"], "deadlines": len(rows), "start_date": start.isoformat() if start else None},
        "findings": findings,
        "extra": {"rows": rows, "csv": "\n".join(csv_lines)},
    }
