"""Ingestion connectors end to end, against a real Smriti server on a free port
serving the built-in demo targets (multi-page site, JSON API, MCP server,
SQLite file, folder). Jobs are run synchronously with run_pending()."""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from smriti import connectors
from smriti.api import create_app
from smriti.ask import ask
from smriti.cli import seed
from smriti.config import Settings
from smriti.connectors.sql import check_query
from smriti.egress import EgressDenied, check_url


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    tmp: Path = tmp_path_factory.mktemp("smriti-connectors")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    old = os.environ.get("SMRITI_SELF_URL")
    os.environ["SMRITI_SELF_URL"] = f"http://127.0.0.1:{port}"
    app = create_app(
        Settings.from_env(data_dir=tmp, database_url=f"sqlite:///{(tmp / 's.db').as_posix()}", scheduler=False)
    )
    plat = app.state.platform
    seed(plat)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120)
    yield {
        "app": app,
        "plat": plat,
        "t": plat.tenant(None),
        "base": f"http://127.0.0.1:{port}",
        "client": client,
        "data": tmp,
    }
    server.should_exit = True
    th.join(timeout=10)
    if old is None:
        os.environ.pop("SMRITI_SELF_URL", None)
    else:
        os.environ["SMRITI_SELF_URL"] = old


def _create(live, name: str, kind: str, values: dict, user: str = "km") -> int:  # type: ignore[no-untyped-def]
    r = live["client"].post(
        "/api/connectors", json={"name": name, "kind": kind, "values": values}, headers={"X-Smriti-User": user}
    )
    assert r.status_code == 200, r.text
    return int(r.json()["id"])


def _run(live, cid: int) -> dict:  # type: ignore[no-untyped-def]
    r = live["client"].post(f"/api/connectors/{cid}/run", headers={"X-Smriti-User": "km"})
    assert r.status_code == 200, r.text
    done = connectors.run_pending([live["t"]], live["plat"].box)
    assert done, "no job ran"
    return done[-1]


def test_website_crawl_scope_documents_and_resync(live):  # type: ignore[no-untyped-def]
    cid = _create(
        live,
        "Engineering handbook",
        "website",
        {
            "start_url": live["base"] + "/demo-site/wiki/",
            "scope": "path",
            "exclude": "login",
            "min_interval_s": 0,
            "max_pages": 30,
            "doc_class": "public",
        },
    )
    job = _run(live, cid)
    urls = {i["url"] for i in job["items"]}
    assert job["status"] == "done", job
    assert any(u.endswith("bridge-inspection-checklist.pdf") for u in urls)  # linked PDF downloaded
    assert not any("login" in u or "/other/" in u or "example.com" in u for u in urls)  # exclude + scope
    assert job["stats"]["ingested"] == 7 and job["stats"]["failed"] == 0  # index, 5 pages, 1 PDF
    r = ask(
        live["t"],
        live["t"].principal("asha"),
        "How is scour depth at piers checked against the danger level?",
        use_cache=False,
    )
    assert not r["abstained"] and "scour" in r["answer"].lower()
    again = _run(live, cid)
    # Re-sync finds nothing new. The home page is reachable as /wiki/ and /wiki/index.html;
    # content-hash dedupe stores it once, so 8 items are found for 7 documents.
    assert job["stats"]["found"] == 8
    assert again["stats"]["ingested"] == 0 and again["stats"]["unchanged"] == 8


def test_rest_api_pagination_and_mapping(live):  # type: ignore[no-untyped-def]
    cid = _create(
        live,
        "Notices API",
        "api",
        {
            "url": live["base"] + "/demo-api/notices",
            "items_path": "data.items",
            "pagination": "page",
            "page_param": "page",
            "size_param": "per_page",
            "page_size": 3,
            "date_field": "published_at",
            "min_interval_s": 0,
            "doc_class": "internal",
        },
    )
    job = _run(live, cid)
    assert job["status"] == "done" and job["stats"]["ingested"] == 7, job  # 3 pages of 3, 3, 1
    t = live["t"]
    doc = next(d for d in t.store.list_documents(t.id) if d["title"] == "Training on drone inspection")
    assert doc["doc_date"] == "2026-07-15" and doc["doc_id"].startswith(f"c{cid}-")


def test_mcp_resources_and_tool(live):  # type: ignore[no-untyped-def]
    cid = _create(live, "Handover notes", "mcp", {"url": live["base"] + "/demo-mcp/mcp", "doc_class": "internal"})
    job = _run(live, cid)
    assert job["status"] == "done" and job["stats"]["ingested"] == 3, job
    r = ask(
        live["t"], live["t"].principal("asha"), "What did the handover note say about bridge 47 scour?", use_cache=False
    )
    assert "47" in r["answer"] and not r["abstained"]
    tool = _create(
        live,
        "Handover search",
        "mcp",
        {
            "url": live["base"] + "/demo-mcp/mcp",
            "mode": "tool",
            "tool_name": "search_handover",
            "tool_args": '{"query": "arbitration"}',
        },
    )
    tjob = _run(live, tool)
    assert tjob["status"] == "done" and tjob["stats"]["ingested"] == 1, tjob


def test_database_rows_and_table_totals(live):  # type: ignore[no-untyped-def]
    db = (live["data"] / "imports" / "db" / "works.db").as_posix()
    rows = _create(
        live,
        "Works register",
        "database",
        {
            "url": f"sqlite:///{db}",
            "query": "SELECT * FROM works_register",
            "id_column": "id",
            "title_column": "work_name",
            "date_column": "updated_at",
            "doc_class": "internal",
        },
    )
    job = _run(live, rows)
    assert job["status"] == "done" and job["stats"]["ingested"] == 5, job
    table = _create(
        live,
        "Works register (table)",
        "database",
        {
            "url": f"sqlite:///{db}",
            "query": "SELECT work_name, division, sanctioned_cost_cr FROM works_register",
            "mode": "table",
            "table_title": "Works register sanctioned cost",
            "doc_class": "internal",
        },
    )
    assert _run(live, table)["stats"]["ingested"] == 1
    r = ask(
        live["t"],
        live["t"].principal("asha"),
        "What is the total sanctioned cost cr of works in NR-DLI division?",
        use_cache=False,
    )
    assert r.get("computed", {}).get("total") == pytest.approx(75.95), r["answer"]


def test_database_is_read_only():
    for bad in (
        "DELETE FROM works",
        "SELECT 1; DROP TABLE works",
        "WITH x AS (DELETE FROM w RETURNING *) SELECT * FROM x",
        "UPDATE w SET a=1",
        "PRAGMA writable_schema=1",
    ):
        with pytest.raises(connectors.ConnectorError):
            check_query(bad)
    assert check_query("SELECT 'update me' AS note FROM works -- trailing comment").startswith("SELECT")


def test_sqlite_outside_allowed_roots_is_refused(live, tmp_path):  # type: ignore[no-untyped-def]
    out = connectors.preview(
        live["t"], "database", {"query": "SELECT 1", "url": f"sqlite:///{(tmp_path / 'x.db').as_posix()}"}
    )
    assert not out["ok"] and "SMRITI_FOLDER_ROOTS" in out["error"]


def test_folder_and_upload(live):  # type: ignore[no-untyped-def]
    cid = _create(
        live, "Site documents", "folder", {"path": str(live["data"] / "imports" / "docs"), "doc_class": "internal"}
    )
    assert _run(live, cid)["stats"]["ingested"] == 2
    files = [
        (
            "files",
            (
                "site-note.md",
                b"# Site note (synthetic)\n\nCuring of the ROB deck slab continues for 14 days.\n",
                "text/markdown",
            ),
        )
    ]
    r = live["client"].post(
        "/api/ingest/upload", files=files, data={"doc_class": "internal"}, headers={"X-Smriti-User": "km"}
    )
    assert r.status_code == 200, r.text
    job = connectors.run_pending([live["t"]], live["plat"].box)[-1]
    assert job["kind"] == "upload" and job["stats"]["ingested"] == 1


def test_access_settings_reach_the_documents(live):  # type: ignore[no-untyped-def]
    cid = _create(
        live,
        "Restricted page",
        "url",
        {
            "url": live["base"] + "/demo-site/wiki/signalling-interface.html",
            "doc_class": "confidential",
            "projects": "P-17",
            "divisions": "NR-DLI",
        },
    )
    job = _run(live, cid)
    doc_id = job["items"][0]["doc_id"]
    c = live["client"]
    assert c.get(f"/api/documents/{doc_id}", headers={"X-Smriti-User": "asha"}).status_code == 404
    assert c.get(f"/api/documents/{doc_id}", headers={"X-Smriti-User": "vikram"}).status_code == 200


def test_secrets_never_leave_the_server(live):  # type: ignore[no-untyped-def]
    cid = _create(
        live,
        "Secret API",
        "api",
        {
            "url": live["base"] + "/demo-api/notices",
            "auth": "bearer",
            "token": "s3cr3t-token-value",
            "items_path": "data.items",
        },
    )
    body = live["client"].get("/api/connectors", headers={"X-Smriti-User": "km"}).text
    assert "s3cr3t-token-value" not in body and '"secrets_set":["token"]' in body.replace(" ", "")
    row = connectors.get_row(live["t"], cid)
    assert "s3cr3t" not in row["secrets"] and "s3cr3t" not in row["config"]
    # updating without re-entering the token keeps it
    live["client"].put(
        f"/api/connectors/{cid}",
        json={"name": "Secret API", "kind": "api", "values": {"url": live["base"] + "/demo-api/notices"}},
        headers={"X-Smriti-User": "km"},
    )
    assert live["plat"].box.open(connectors.get_row(live["t"], cid)["secrets"]) == {"token": "s3cr3t-token-value"}


def test_only_managers_configure_sources(live):  # type: ignore[no-untyped-def]
    r = live["client"].post(
        "/api/connectors",
        json={"name": "x", "kind": "url", "values": {"url": "https://example.com"}},
        headers={"X-Smriti-User": "asha"},
    )
    assert r.status_code == 403
    r = live["client"].post(
        "/api/ingest/upload", files=[("files", ("a.md", b"# a", "text/markdown"))], headers={"X-Smriti-User": "asha"}
    )
    assert r.status_code == 403


def test_cancel_before_run(live):  # type: ignore[no-untyped-def]
    cid = _create(live, "To cancel", "url", {"url": live["base"] + "/demo-site/wiki/suraksha.html"})
    jid = live["client"].post(f"/api/connectors/{cid}/run", headers={"X-Smriti-User": "km"}).json()["job_id"]
    live["client"].post(f"/api/jobs/{jid}/cancel", headers={"X-Smriti-User": "km"})
    assert live["client"].get(f"/api/jobs/{jid}", headers={"X-Smriti-User": "km"}).json()["status"] == "cancelled"
    assert connectors.run_pending([live["t"]], live["plat"].box) == []


def test_preview_ingests_nothing(live):  # type: ignore[no-untyped-def]
    t = live["t"]
    before = len(t.store.list_documents(t.id))
    out = (
        live["client"]
        .post(
            "/api/connectors/preview",
            json={
                "kind": "website",
                "values": {"start_url": live["base"] + "/demo-site/wiki/", "min_interval_s": 0, "max_pages": 3},
            },
            headers={"X-Smriti-User": "km"},
        )
        .json()
    )
    assert out["ok"] and out["items"] and out["items"][0].get("snippet")
    assert len(t.store.list_documents(t.id)) == before


def test_egress_guard(live):  # type: ignore[no-untyped-def]
    for bad in (
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        live["base"] + "/api/documents?as=nadia",
        "http://127.0.0.1:6333/collections",
    ):
        with pytest.raises(EgressDenied):
            check_url(bad)
    check_url(live["base"] + "/demo-site/wiki/")  # Smriti's own sample targets only
