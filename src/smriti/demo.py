"""Built-in sample targets for every connector kind, so ingestion can be tried
(and tested) without any external system. All content is synthetic.

    /demo-site/wiki/              a multi-page handbook with a PDF and links out of scope
    /demo-api/notices             a paginated JSON API
    /demo-mcp/mcp                 an MCP server (streamable HTTP) with handover notes and a search tool
    <data>/imports/db/works.db    a SQLite works register
    <data>/imports/docs/          a folder of files
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

WIKI: dict[str, tuple[str, str]] = {
    "track-inspection": (
        "Track inspection (synthetic handbook)",
        "<p>Keymen shall inspect their beat daily on foot. Gauge, cross-level and alignment are recorded weekly on the trolley inspection sheet.</p>"
        "<p>Rail fractures found during inspection are protected at once with hand signals at 600 metres on either side.</p>",
    ),
    "bridge-inspection": (
        "Bridge inspection (synthetic handbook)",
        "<p>Every major bridge is inspected by the Assistant Engineer before and after the monsoon. Scour around piers is measured with a sounding rod and recorded against the danger level.</p>"
        '<p>The full checklist is in the <a href="bridge-inspection-checklist.pdf">bridge inspection checklist (PDF)</a>.</p>',
    ),
    "monsoon-patrolling": (
        "Monsoon patrolling (synthetic handbook)",
        "<p>Monsoon patrolling starts on 15 June. Patrolmen walk vulnerable sections at night during heavy rain and report water levels at bridges every two hours.</p>",
    ),
    "signalling-interface": (
        "Signalling interface for engineering works (synthetic handbook)",
        "<p>No track work affecting points or track circuits shall start without a disconnection notice signed by the signal maintainer.</p>",
    ),
    "suraksha": (
        "सुरक्षा नियम (synthetic handbook)",
        "<p>कार्य स्थल पर सभी कर्मचारी हेलमेट और परावर्तक जैकेट पहनेंगे। ब्लॉक के बिना कोई भी मशीन पटरी पर नहीं चलाई जाएगी।</p>",
    ),
}

NOTICES = [
    {"id": 101 + i, "title": t, "published_at": d, "department": dep, "body": b, "status": "published"}
    for i, (t, d, dep, b) in enumerate(
        [
            (
                "Revised schedule of rates for earthwork",
                "2026-01-12",
                "Engineering",
                "The schedule of rates for earthwork in formation is revised by 4% for works sanctioned after 1 February 2026.",
            ),
            (
                "E-tender registration reminder",
                "2026-02-03",
                "Stores",
                "All contractors must renew their e-tendering portal registration before 31 March 2026.",
            ),
            (
                "Bridge rehabilitation priority list",
                "2026-03-18",
                "Engineering",
                "Seven distressed bridges on the Delhi division are placed in the 2026-27 rehabilitation programme.",
            ),
            (
                "Land acquisition: objections hearing",
                "2026-04-07",
                "Land",
                "Objections to the notification for project P-17 will be heard on 28 April 2026 at the divisional office.",
            ),
            (
                "Safety drive during monsoon",
                "2026-05-25",
                "Safety",
                "A safety drive on monsoon preparedness runs from 1 to 30 June 2026 across all sections.",
            ),
            (
                "Revised performance guarantee format",
                "2026-06-02",
                "Finance",
                "Performance guarantees shall be submitted in the revised bank guarantee format from 1 July 2026.",
            ),
            (
                "Training on drone inspection",
                "2026-07-15",
                "Engineering",
                "Assistant Engineers will be trained in drone-based bridge inspection in batches of twenty.",
            ),
        ]
    )
]

HANDOVER = {
    "notes://handover/rohtak-bridges": (
        "Handover note: Rohtak section bridges",
        "Bridge 47 (span 3) has recurring scour on the downstream side; the boulder apron was renewed in 2024 and needs checking after every heavy spell. "
        "The danger-level marking on bridge 52 was repainted in May 2026.",
    ),
    "notes://handover/contract-disputes": (
        "Handover note: pending contract disputes",
        "The arbitration on tender NR-DLI-ENG-2023-07 concerns the late fee under clause 16.2; the case file is with the Deputy Chief Engineer. "
        "Two claims for extension of time under clause 17 are awaiting finance vetting.",
    ),
    "notes://handover/land-p17": (
        "Handover note: land acquisition for P-17",
        "The village Rampur schedule is final; Kheri awaits two objections. Compensation for plot P17-005 is held pending a court order.",
    ),
}


def make_pdf(lines: list[str]) -> bytes:
    """A minimal valid one-page PDF with a text layer (Helvetica)."""
    esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    content = "BT /F1 11 Tf 56 780 Td 15 TL " + " ".join(f"({ln}) Tj T*" for ln in esc) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


CHECKLIST_PDF = make_pdf(
    [
        "BRIDGE INSPECTION CHECKLIST (synthetic demo)",
        "1. Check bearings for rust, displacement and missing bolts.",
        "2. Measure scour depth at every pier against the danger level.",
        "3. Inspect girders for cracks near the bearing stiffeners.",
        "4. Record waterway obstruction and vegetation growth.",
        "5. Photograph each defect and enter it in the bridge register within 7 days.",
    ]
)


def _page(title: str, body: str) -> str:
    nav = " | ".join(f'<a href="{k}.html">{v[0].split(" (")[0]}</a>' for k, v in WIKI.items())
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body>"
        f'<nav><a href="index.html">Handbook home</a> | {nav} | <a href="/demo-site/wiki/login.html">Login</a></nav>'
        f"<main><h1>{title}</h1>{body}</main>"
        '<footer><a href="https://example.com/external">External link (out of scope)</a> · '
        '<a href="/demo-site/other/unrelated.html">Unrelated section</a></footer></body></html>'
    )


def seed_files(data_dir: Path) -> None:
    """The demo database and folder that the database and folder connectors read."""
    db = data_dir / "imports" / "db" / "works.db"
    if not db.exists():
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE works_register (id INTEGER PRIMARY KEY, work_name TEXT, division TEXT, sanctioned_cost_cr REAL, status TEXT, remarks TEXT, updated_at TEXT)"
        )
        con.executemany(
            "INSERT INTO works_register VALUES (?,?,?,?,?,?,?)",
            [
                (
                    1,
                    "Doubling Shakurbasti-Rohtak (earthwork)",
                    "NR-DLI",
                    42.50,
                    "in progress",
                    "LOA issued June 2025; PG received",
                    "2026-05-02",
                ),
                (
                    2,
                    "Platform extension at Barabanki",
                    "NR-LKO",
                    11.75,
                    "tendered",
                    "bids under evaluation",
                    "2026-04-20",
                ),
                (3, "ROB at km 23/4 Rohtak", "NR-DLI", 28.10, "sanctioned", "GAD approved by state PWD", "2026-03-11"),
                (
                    4,
                    "Rehabilitation of bridge 47",
                    "NR-DLI",
                    3.40,
                    "in progress",
                    "scour protection with boulder apron",
                    "2026-06-01",
                ),
                (
                    5,
                    "Station building, Kheri halt",
                    "NR-DLI",
                    1.95,
                    "completed",
                    "handed over to operations",
                    "2026-01-30",
                ),
            ],
        )
        con.commit()
        con.close()
    docs = data_dir / "imports" / "docs"
    if not docs.exists():
        docs.mkdir(parents=True)
        (docs / "site-order-book-rohtak.md").write_text(
            "# Site order book, Rohtak doubling (synthetic)\n\n12.05.2026: Blanketing layer thickness found 550 mm against 600 mm specified at km 18/2; contractor directed to rectify before ballast.\n",
            encoding="utf-8",
        )
        (docs / "quality-audit-2026.md").write_text(
            "# Quality audit of earthwork, 2026 (synthetic)\n\nCompaction tests at 12 of 14 locations met 98% of maximum dry density; two locations at km 21 were rejected and re-rolled.\n",
            encoding="utf-8",
        )


def mount(app: FastAPI) -> Any:
    """Adds the demo routes; returns the demo MCP server (or None) whose session
    manager the app lifespan must run."""

    @app.get("/demo-site/wiki/", response_class=HTMLResponse)
    @app.get("/demo-site/wiki/index.html", response_class=HTMLResponse)
    def wiki_index() -> str:
        items = "".join(f'<li><a href="{k}.html">{v[0]}</a></li>' for k, v in WIKI.items())
        return _page(
            "Track and Bridge Engineering Handbook (synthetic)", f"<p>Sections of the handbook:</p><ul>{items}</ul>"
        )

    @app.get("/demo-site/wiki/{name}.html", response_class=HTMLResponse)
    def wiki_page(name: str) -> str:
        if name == "login":
            return _page("Login", "<p>Please sign in.</p>")
        if name not in WIKI:
            raise HTTPException(404)
        return _page(*WIKI[name])

    @app.get("/demo-site/wiki/bridge-inspection-checklist.pdf")
    def wiki_pdf() -> Response:
        return Response(CHECKLIST_PDF, media_type="application/pdf")

    @app.get("/demo-site/other/unrelated.html", response_class=HTMLResponse)
    def other() -> str:
        return _page(
            "Unrelated section", "<p>This page is outside the handbook and should not be crawled with path scope.</p>"
        )

    @app.get("/demo-api/notices")
    def notices(request: Request, page: int = 1, per_page: int = 3) -> dict[str, Any]:
        per_page = max(1, min(per_page, 50))
        start = (page - 1) * per_page
        chunk = NOTICES[start : start + per_page]
        return {"data": {"items": chunk}, "meta": {"page": page, "per_page": per_page, "total": len(NOTICES)}}

    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        return None

    srv = MCPServer("smriti-demo-handover", instructions="Synthetic handover notes from retiring officers.")

    def reader_for(title: str, text: str) -> Any:
        def reader() -> str:  # no parameters: MCP maps handler parameters to URI template variables
            return f"# {title}\n\n{text}\n"

        return reader

    for uri, (title, text) in HANDOVER.items():
        srv.resource(uri, name=uri.rsplit("/", 1)[-1], title=title, description=title, mime_type="text/markdown")(
            reader_for(title, text)
        )

    from mcp import types as mcp_types

    @srv.tool(annotations=mcp_types.ToolAnnotations(read_only_hint=True))
    def search_handover(query: str) -> str:
        """Search the handover notes; returns matching notes."""
        q = query.lower()
        hits = [f"## {t}\n{x}" for t, x in HANDOVER.values() if any(w in (t + x).lower() for w in q.split())]
        return "\n\n".join(hits) or "No matching notes."

    @srv.tool()
    def request_handover_meeting(officer: str, topic: str) -> str:
        """Ask a retiring officer for a handover meeting on a topic (demo: nothing is sent)."""
        return f"Meeting request recorded for {officer} on '{topic}'. Reference HM-{abs(hash((officer, topic))) % 10000:04d} (demo)."

    # Keep the SDK's DNS-rebinding protection, but accept Smriti's own configured
    # hostname as well as localhost (in Docker the worker calls http://smriti-api:8000).
    from urllib.parse import urlsplit

    from mcp.server.transport_security import TransportSecuritySettings

    hosts = {"127.0.0.1", "localhost"}
    for env in ("SMRITI_SELF_URL", "SMRITI_PUBLIC_URL"):
        if h := urlsplit(os.environ.get(env, "")).hostname:
            hosts.add(h)
    security = TransportSecuritySettings(
        allowed_hosts=sorted({p for h in hosts for p in (h, f"{h}:*")}),
        allowed_origins=sorted({f"{s}://{h}:*" for h in hosts for s in ("http", "https")}),
    )
    # stateless: the API runs several worker processes, and in-memory MCP sessions would not
    # survive a follow-up request landing on another process
    app.mount(
        "/demo-mcp", srv.streamable_http_app(transport_security=security, stateless_http=True, json_response=True)
    )
    return srv
