"""Sentence-by-sentence verification of pasted text against the library."""

from __future__ import annotations

import re
from typing import Any

from smriti.guardrails import CITE
from smriti.index import split_sentences, tokenize
from smriti.skilllib import parse_date

MAX_SENTENCES = 40


def run(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    text = str(inputs.get("text") or "")
    as_of = parse_date(inputs.get("as_of"))
    sents = [CITE.sub("", re.sub(r"\s+", " ", s)).strip() for s in split_sentences(text)]
    sents = [s for s in sents if len(tokenize(s)) >= 3][:MAX_SENTENCES]
    if not sents:
        return {
            "findings": [
                {"check": "text", "severity": "major", "message": "Paste the text to check.", "citation": None}
            ]
        }
    findings: list[dict[str, Any]] = []
    tally = {"supported": 0, "unsupported": 0, "number_mismatch": 0, "negation_mismatch": 0, "superseded_only": 0}
    for i, s in enumerate(sents, start=1):
        ps = ctx.search(s, as_of=as_of.isoformat() if as_of else None, top_k=3)
        short = s if len(s) <= 160 else s[:157] + "…"
        if not ps:
            tally["unsupported"] += 1
            findings.append(
                {
                    "check": f"sentence_{i}",
                    "severity": "major",
                    "message": f'Nothing in the library supports: "{short}"',
                    "citation": None,
                }
            )
            continue
        # citation marks go before the full stop, or the splitter files them as a sentence of their own
        v = ctx.tenant.guard.verify(s.rstrip(" .!?;") + " " + "".join(f"[{p.n}]" for p in ps) + ".", ps)
        row = (
            v["sentences"][0]
            if v["sentences"]
            else {"supported": False, "numbers_not_in_source": [], "polarity_ok": True}
        )
        best = max(ps, key=lambda p: len(set(tokenize(s)) & set(tokenize(p.text))))
        cite = ctx.cite(best)
        current = [p for p in ps if p.status != "superseded"]
        if row["numbers_not_in_source"]:
            tally["number_mismatch"] += 1
            nums = ", ".join(row["numbers_not_in_source"])
            findings.append(
                {
                    "check": f"sentence_{i}",
                    "severity": "critical",
                    "message": f'Number not in the source ({nums}): "{short}"',
                    "citation": cite,
                }
            )
        elif not row["polarity_ok"]:
            tally["negation_mismatch"] += 1
            findings.append(
                {
                    "check": f"sentence_{i}",
                    "severity": "critical",
                    "message": f'The source says the opposite: "{short}"',
                    "citation": cite,
                }
            )
        elif not row["supported"]:
            tally["unsupported"] += 1
            findings.append(
                {
                    "check": f"sentence_{i}",
                    "severity": "major",
                    "message": f'Not supported by the closest passages: "{short}"',
                    "citation": cite,
                }
            )
        elif not current:
            tally["superseded_only"] += 1
            findings.append(
                {
                    "check": f"sentence_{i}",
                    "severity": "major",
                    "message": f'Supported only by a superseded rule: "{short}"',
                    "citation": cite,
                }
            )
        else:
            tally["supported"] += 1
            findings.append(
                {"check": f"sentence_{i}", "severity": "pass", "message": f'Supported: "{short}"', "citation": cite}
            )
    return {
        "fields": {"sentences": len(sents), **tally, "as_of": as_of.isoformat() if as_of else None},
        "findings": findings,
    }
