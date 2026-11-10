"""RTI reply draft: one cited answer per point, deadline and exemption checks."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from smriti.index import tokenize
from smriti.privacy import pii_categories
from smriti.skilllib import parse_date

INVESTIGATION = r"vigilance|investigat|inquiry|enquiry|prosecut|charge[- ]?sheet|FIR\b"
COMMERCIAL = r"rates? quoted|bid price|financial bid|trade secret|commercial confiden|L1\b"
SECURITY = r"national security|security of the state|defence|strategic|classified"
PERSONAL = r"personal|aadhaar|\bPAN\b|home address|medical|salary of|property returns?"
EXEMPTIONS = (
    ("8(1)(h)", "would impede investigation or prosecution", INVESTIGATION),
    ("8(1)(d)", "commercial confidence or trade secrets of a third party", COMMERCIAL),
    ("8(1)(a)", "security or strategic interests", SECURITY),
    ("8(1)(e)", "held in a fiduciary relationship", r"fiduciary"),
    ("8(1)(j)", "personal information with no public interest", PERSONAL),
)
MASK_TOKEN = re.compile(r"\[[A-Z][A-Z_]+\]")
# share of the point's content words a passage must contain before it is quoted as the answer
MIN_OVERLAP = 0.5
QUOTES_PER_POINT = 2  # a rule and the slip that amends it usually both matter
STOP = {"what", "which", "whom", "copy", "copies", "details", "detail", "information", "provide", "please", "kindly",
        "number", "list", "give", "under", "about", "regarding", "the", "and", "for", "from", "with", "this", "that"}  # fmt: skip


def _relevant(q: str, text: str) -> bool:
    words = {w for w in tokenize(q) if len(w) > 2 and w not in STOP}
    return bool(words) and len(words & set(tokenize(text))) / len(words) >= MIN_OVERLAP


def _exemptions(text: str) -> list[tuple[str, str]]:
    return [(sec, why) for sec, why, pat in EXEMPTIONS if re.search(pat, text, re.I)]


def run(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    received = parse_date(inputs.get("received_date"))
    today = parse_date(inputs.get("as_of")) or dt.date.today()
    urgent = str(inputs.get("life_or_liberty") or "").strip().lower() in ("yes", "y", "true", "1")
    points = [
        re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", q).strip() for q in str(inputs.get("questions") or "").splitlines()
    ]
    points = [q for q in points if len(q) > 3]
    findings: list[dict[str, Any]] = []
    if not received:
        findings.append(
            {
                "check": "received_date",
                "severity": "major",
                "message": "Give the date the application was received.",
                "citation": None,
            }
        )
    if not points:
        findings.append(
            {
                "check": "questions",
                "severity": "major",
                "message": "List the information sought, one point per line.",
                "citation": None,
            }
        )
        return {"findings": findings}
    due = None
    if received:
        due = received + (dt.timedelta(days=2) if urgent else dt.timedelta(days=30))
        left = (due - today).days
        rule = "48 hours under the proviso to section 7(1)" if urgent else "30 days under section 7(1)"
        if left < 0:
            findings.append(
                {
                    "check": "deadline",
                    "severity": "critical",
                    "message": f"Reply was due on {due.isoformat()} ({rule}); it is {-left} days late. Inaction is a deemed refusal under section 7(2).",
                    "citation": None,
                }
            )
        elif left <= 5:
            findings.append(
                {
                    "check": "deadline",
                    "severity": "major",
                    "message": f"Reply due on {due.isoformat()} ({rule}), {left} days left.",
                    "citation": None,
                }
            )
        else:
            findings.append(
                {
                    "check": "deadline",
                    "severity": "pass",
                    "message": f"Reply due on {due.isoformat()} ({rule}).",
                    "citation": None,
                }
            )

    answers: list[str] = []
    for i, q in enumerate(points, start=1):
        # a passage flagged for prompt injection is never quoted into an outgoing letter
        hits = [h for h in ctx.search(q, top_k=5) if not h.injection_flag and _relevant(q, h.text)]
        flags = _exemptions(q)
        if hits:
            quotes = []
            for p in hits[:QUOTES_PER_POINT]:
                c = ctx.cite(p)
                text = re.sub(r"\s+", " ", p.text).strip()
                flags += [f for f in _exemptions(text) if f not in flags]
                if (MASK_TOKEN.search(text) or pii_categories(text)) and ("8(1)(j)", EXEMPTIONS[4][1]) not in flags:
                    flags.append(("8(1)(j)", EXEMPTIONS[4][1]))
                src = " ".join(
                    x for x in (p.title, p.ref_no and f"({p.ref_no})", p.clause_id and f"clause {p.clause_id}") if x
                )
                quotes.append(f'As per {src}: "{text[:700]}"')
                findings.append(
                    {
                        "check": f"point_{i}",
                        "severity": "info",
                        "message": f"Point {i} answered from {src}.",
                        "citation": c,
                    }
                )
            answers.append(f"{i}. {q}\n   Reply: " + "\n   ".join(quotes))
        else:
            answers.append(
                f"{i}. {q}\n   Reply: The information sought is not held by this public authority. [[TO FILL: confirm, or name the public authority to transfer to under section 6(3) within 5 days]]"
            )
            findings.append(
                {
                    "check": f"point_{i}",
                    "severity": "minor",
                    "message": f"Point {i}: nothing found in the library you can open. Confirm it is not held, or transfer under section 6(3).",
                    "citation": None,
                }
            )
        for sec, why in flags:
            findings.append(
                {
                    "check": f"exemption_{sec}",
                    "severity": "major",
                    "message": f"Point {i} may fall under section {sec} ({why}). Decide before release; if you withhold, give reasons and consider severance under section 10.",
                    "citation": None,
                }
            )

    ref = str(inputs.get("applicant_reference") or "").strip() or "[[TO FILL: registration number]]"
    draft = "\n".join(
        [
            "Subject: Information sought under the Right to Information Act, 2005",
            f"Reference: {ref}, received on {received.isoformat() if received else '[[TO FILL: date of receipt]]'}",
            "",
            "With reference to the above application, point-wise information is furnished below.",
            "",
            *answers,
            "",
            "If you are not satisfied with this reply, you may file a first appeal under section 19(1) within 30 days of receipt of this letter to the First Appellate Authority: [[TO FILL: name, designation and address of the First Appellate Authority]].",
            "",
            "[[TO FILL: name and designation of the CPIO]]",
            "Central Public Information Officer",
        ]
    )
    return {
        "fields": {"points": len(points), "due_date": due.isoformat() if due else None, "life_or_liberty": urgent},
        "findings": findings,
        "draft": draft,
    }
