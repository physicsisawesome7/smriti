"""Connector framework.

A connector *discovers* items; each item knows how to fetch its bytes. The job
runner feeds every item through the same ingest pipeline as uploads (parse,
version, mask, chunk, embed, supersession), so a document from a website, a
REST API, a database row or an MCP server is governed exactly like a PDF.

A connector declares its configuration as a list of fields; the web app
renders the form from that list, so adding a connector kind needs no UI code.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from ..egress import guarded_client

USER_AGENT = "SmritiBot/0.2 (+https://safearly.com/smriti/bot; institutional-memory connector)"

# MIME type -> filename extension the parser router understands
EXT_BY_MIME = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "message/rfc822": ".eml",
    "application/zip": ".zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
DOC_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip", ".md", ".txt", ".eml", ".html", ".htm")


class ConnectorError(RuntimeError):
    """A configuration or connectivity problem shown to the person running the job."""


@dataclass
class Item:
    key: str  # stable identity within this source, used for change and removal detection
    filename: str
    fetch: Callable[[], bytes]
    title: str | None = None
    url: str | None = None
    doc_id: str | None = None  # explicit document id (rows, API records, MCP resources)
    hints: dict[str, Any] = field(default_factory=dict)  # title, date, doc_type, ...
    size: int | None = None


@dataclass
class Ctx:
    config: dict[str, Any]
    secrets: dict[str, Any]
    state: dict[str, Any]
    log: Callable[[str], None] = print
    cancelled: Callable[[], bool] = lambda: False
    connector_id: int | None = None
    tenant: Any = None  # the Tenant, for kinds that need the store (uploads)


def F(
    name: str,
    label: str,
    type: str = "text",
    *,
    required: bool = False,
    default: Any = None,
    help: str = "",
    options: list[str] | None = None,
    group: str = "Connection",
    placeholder: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": type,
        "required": required,
        "default": default,
        "help": help,
        "options": options or [],
        "group": group,
        "placeholder": placeholder,
    }


class Connector:
    kind: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    icon: ClassVar[str] = "◇"
    fields: ClassVar[list[dict[str, Any]]] = []
    uses_network: ClassVar[bool] = True

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.cfg = {f["name"]: f.get("default") for f in self.fields}
        self.cfg.update({k: v for k, v in ctx.config.items() if v not in (None, "")})
        self.secrets = ctx.secrets
        self._client: httpx.Client | None = None
        self._last: dict[str, float] = {}
        self._robots: dict[str, Any] = {}
        self.errors = 0  # discovery requests that failed; removal detection is skipped when any did
        self._lock = threading.Lock()

    def discover(self) -> Iterator[Item]:
        raise NotImplementedError

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ---------------------------------------------------------------- helpers

    def validate(self) -> None:
        missing = [
            f["label"] for f in self.fields if f["required"] and f["type"] != "secret" and not self.cfg.get(f["name"])
        ]
        missing += [
            f["label"]
            for f in self.fields
            if f["required"] and f["type"] == "secret" and not self.secrets.get(f["name"])
        ]
        if missing:
            raise ConnectorError("missing: " + ", ".join(missing))

    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            self._client = guarded_client(
                headers={"User-Agent": USER_AGENT, **self.extra_headers()},
                timeout=httpx.Timeout(float(self.cfg.get("timeout_s") or 30), connect=10.0),
                follow_redirects=True,
                verify=bool(self.cfg.get("verify_tls", True)),
            )
        return self._client

    def extra_headers(self) -> dict[str, str]:
        return {}

    def polite_get(
        self, url: str, *, min_interval: float = 1.0, respect_robots: bool = True, **kw: Any
    ) -> httpx.Response:
        host = urlsplit(url).netloc
        if respect_robots and not self.robots_allows(url):
            raise ConnectorError(f"robots.txt disallows {url}")
        with self._lock:
            wait = self._last.get(host, 0) + min_interval - time.monotonic()
            self._last[host] = max(time.monotonic(), self._last.get(host, 0) + min_interval)
        if wait > 0:
            time.sleep(wait)
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self.http.get(url, **kw)
                if r.status_code in (429, 502, 503, 504):
                    raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                time.sleep(min(10, 1.5 * 2**attempt))
        raise ConnectorError(f"{url}: {last}")

    def robots_allows(self, url: str) -> bool:
        s = urlsplit(url)
        base = f"{s.scheme}://{s.netloc}"
        if base not in self._robots:
            from ..watch import robots_policy

            try:
                r = self.http.get(base + "/robots.txt", timeout=10)
                self._robots[base] = robots_policy(r.status_code, r.text)
            except httpx.HTTPError:
                self._robots[base] = False  # unreachable: disallow for this run (RFC 9309)
        rp = self._robots[base]
        return rp is not False and (rp is True or rp.can_fetch(USER_AGENT, url))


def filename_for(url: str, content_type: str | None, fallback: str = "page") -> str:
    path = urlsplit(url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1] if path else ""
    if name and "." in name and name.lower().endswith(DOC_EXTS):
        return name
    ext = EXT_BY_MIME.get((content_type or "").split(";")[0].strip().lower(), ".html")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name or fallback).strip("-") or fallback
    return stem + ext


def check_max_bytes(data: bytes, cfg: dict[str, Any], what: str) -> bytes:
    limit = int(cfg.get("max_file_mb") or 100) * 1024 * 1024
    if len(data) > limit:
        raise ConnectorError(f"{what}: larger than {limit // (1024 * 1024)} MB")
    return data


COMMON_FIELDS = [
    F(
        "doc_class",
        "Sensitivity",
        "select",
        default="internal",
        options=["public", "internal", "confidential", "secret"],
        group="Access",
    ),
    F(
        "divisions",
        "Divisions",
        default="*",
        help="comma separated; * means everyone in the organisation",
        group="Access",
    ),
    F("projects", "Projects", help="comma separated; empty means not project-restricted", group="Access"),
    F("groups", "Need-to-know groups", help="comma separated; empty means none required", group="Access"),
    F("doc_type", "Document type", help="optional; classified automatically when empty", group="Access"),
    F(
        "schedule",
        "Re-sync schedule",
        "select",
        default="",
        options=["", "*/30 * * * *", "0 * * * *", "0 */6 * * *", "0 2 * * *", "0 2 * * 1"],
        help="cron; empty = only when run by hand",
        group="Schedule",
    ),
    F("max_items", "Max items per run", "number", default=500, group="Schedule"),
    F(
        "on_removed",
        "When an item disappears",
        "select",
        default="keep",
        options=["keep", "tombstone"],
        help="keep the archived copy, or tombstone it so it stops appearing in answers",
        group="Schedule",
    ),
    F(
        "removal_misses",
        "Runs an item must be missing before it is tombstoned",
        "number",
        default=3,
        help="a site outage or a failed run never counts as a miss",
        group="Schedule",
    ),
    F(
        "apply_relations",
        "Apply supersession and amendments found in these documents without review",
        "bool",
        default=False,
        help="off: a curator approves each proposed supersession under Library",
        group="Schedule",
    ),
    F(
        "legal_status",
        "Legal status",
        default="customer-owned",
        help="e.g. customer-owned, GODL-India, licensed copy",
        group="Schedule",
    ),
]
