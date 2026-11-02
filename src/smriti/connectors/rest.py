"""REST/JSON API connector.

Pulls records from any JSON API, follows its pagination, and turns each record
into a document (title, date and URL mapped from fields; every other field
rendered as text). When the API returns a file instead of JSON (a PDF report
endpoint), the file is ingested as is.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin

from ..metadata import to_iso
from .base import COMMON_FIELDS, Connector, ConnectorError, F, Item, check_max_bytes, filename_for

MAX_RECORD_CHARS = 200_000


def get_path(obj: Any, path: str | None) -> Any:
    """`data.items`, `results[0].docs`, or empty for the object itself."""
    if not path:
        return obj
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if obj is None:
            return None
        if part.startswith("["):
            idx = int(part[1:-1])
            obj = obj[idx] if isinstance(obj, list) and len(obj) > idx else None
        else:
            obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def render_record(rec: dict[str, Any], title: str, skip: set[str], only: list[str] | None) -> str:
    lines = [f"# {title}", ""]
    keys = only or [k for k in rec if k not in skip]
    for k in keys:
        v = get_path(rec, k)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, indent=1)
        text = str(v)
        if "\n" in text or len(text) > 200:
            lines += [f"## {k}", "", text, ""]
        else:
            lines.append(f"{k}: {text}")
    return "\n".join(lines)[:MAX_RECORD_CHARS]


class ApiConnector(Connector):
    kind = "api"
    label = "REST / JSON API"
    description = "Calls an HTTP API, follows its pagination and ingests each record (or the returned file). Works with most internal and government APIs."
    icon = "⇄"
    fields = [
        F("url", "Endpoint URL", "url", required=True, placeholder="https://api.example.gov.in/v1/notices"),
        F("method", "Method", "select", default="GET", options=["GET", "POST"]),
        F("auth", "Authentication", "select", default="none", options=["none", "bearer", "header", "basic", "query"]),
        F("token", "Token / API key / password", "secret", help="stored encrypted; never shown again"),
        F("header_name", "Header or query parameter name", default="X-API-Key", help="for header/query auth"),
        F("username", "Username", help="for basic auth"),
        F("params", "Extra query parameters (JSON)", "json", placeholder='{"status": "published"}'),
        F("body", "Request body (JSON, POST only)", "json"),
        F(
            "items_path",
            "Path to the list of records",
            group="Mapping",
            placeholder="data.items",
            help="empty when the response itself is the list",
        ),
        F("id_field", "Record id field", default="id", group="Mapping"),
        F("title_field", "Title field", default="title", group="Mapping"),
        F("date_field", "Date field", group="Mapping", placeholder="published_at"),
        F("url_field", "Link field", group="Mapping", help="a URL per record"),
        F(
            "download_url_field",
            "Download the linked file instead of the record",
            "bool",
            default=False,
            group="Mapping",
            help="for APIs that list PDFs",
        ),
        F("text_fields", "Fields to include (comma separated)", group="Mapping", help="empty = all fields"),
        F(
            "pagination",
            "Pagination",
            "select",
            default="none",
            options=["none", "page", "offset", "cursor", "next_url"],
            group="Pagination",
        ),
        F("page_param", "Page / offset / cursor parameter", default="page", group="Pagination"),
        F("start_page", "First page number", "number", default=1, group="Pagination"),
        F("size_param", "Page-size parameter", default="per_page", group="Pagination"),
        F("page_size", "Page size", "number", default=50, group="Pagination"),
        F("next_path", "Path to next cursor / next URL", group="Pagination", placeholder="meta.next"),
        F("max_pages", "Max pages", "number", default=20, group="Pagination"),
        F("min_interval_s", "Seconds between requests", "number", default=0.5, group="Pagination"),
        F("max_file_mb", "Max file size (MB)", "number", default=100, group="Pagination"),
        *COMMON_FIELDS,
    ]

    def extra_headers(self) -> dict[str, str]:
        tok = self.secrets.get("token", "")
        auth = self.cfg.get("auth")
        if auth == "bearer" and tok:
            return {"Authorization": f"Bearer {tok}"}
        if auth == "header" and tok:
            return {self.cfg.get("header_name") or "X-API-Key": tok}
        if auth == "basic":
            raw = f"{self.cfg.get('username') or ''}:{tok}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        return {"Accept": "application/json"}

    def _json_cfg(self, name: str) -> dict[str, Any]:
        v = self.cfg.get(name)
        if not v:
            return {}
        if isinstance(v, dict):
            return v
        try:
            out = json.loads(v)
        except ValueError as e:
            raise ConnectorError(f"{name}: invalid JSON ({e})") from e
        if not isinstance(out, dict):
            raise ConnectorError(f"{name}: must be a JSON object")
        return out

    def _request(self, url: str, params: dict[str, Any]) -> Any:
        import time

        time.sleep(float(self.cfg.get("min_interval_s") or 0))
        if self.cfg.get("auth") == "query" and self.secrets.get("token"):
            params = {**params, (self.cfg.get("header_name") or "api_key"): self.secrets["token"]}
        if self.cfg.get("method") == "POST":
            r = self.http.post(url, params=params, json=self._json_cfg("body") or None)
        else:
            r = self.http.get(url, params=params)
        if r.status_code >= 400:
            raise ConnectorError(f"{url}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def discover(self) -> Iterator[Item]:
        url = self.cfg["url"].strip()
        base_params = self._json_cfg("params")
        mode = self.cfg.get("pagination") or "none"
        page_param = self.cfg.get("page_param") or "page"
        size = int(self.cfg.get("page_size") or 50)
        page: Any = int(self.cfg.get("start_page") or 1) if mode == "page" else 0 if mode == "offset" else None
        next_url: str | None = url
        for n in range(int(self.cfg.get("max_pages") or 20)):
            if self.ctx.cancelled() or not next_url:
                return
            params = dict(base_params) if next_url == url else {}
            if mode in ("page", "offset", "cursor") and page is not None:
                params[page_param] = page
            if mode in ("page", "offset") and self.cfg.get("size_param"):
                params[self.cfg["size_param"]] = size
            r = self._request(next_url, params)
            ctype = (r.headers.get("content-type") or "").split(";")[0].lower()
            if "json" not in ctype:  # the endpoint returned a document
                data = check_max_bytes(r.content, self.cfg, url)
                yield Item(
                    key=url,
                    filename=filename_for(url, ctype, "api-response"),
                    fetch=lambda d=data: d,
                    url=url,
                    size=len(data),
                )
                return
            body = r.json()
            records = get_path(body, self.cfg.get("items_path"))
            if records is None:
                raise ConnectorError(
                    f"no records at path '{self.cfg.get('items_path')}' (top-level keys: {list(body)[:10] if isinstance(body, dict) else type(body).__name__})"
                )
            if isinstance(records, dict):
                records = [records]
            for rec in records:
                if isinstance(rec, dict):
                    yield self._item(rec)
            if not records or mode == "none":
                return
            if mode == "page":
                page = int(page) + 1
            elif mode == "offset":
                page = int(page) + len(records)
            elif mode == "cursor":
                page = get_path(body, self.cfg.get("next_path"))
                if not page:
                    return
            elif mode == "next_url":
                nxt = get_path(body, self.cfg.get("next_path")) or _link_next(r.headers.get("link", ""))
                next_url = urljoin(url, nxt) if nxt else None
            if mode in ("page", "offset") and len(records) < size:
                return
            if n == int(self.cfg.get("max_pages") or 20) - 1:
                self.ctx.log("stopped at max_pages")

    def _item(self, rec: dict[str, Any]) -> Item:
        rid = get_path(rec, self.cfg.get("id_field") or "id")
        title = str(get_path(rec, self.cfg.get("title_field") or "title") or rid or "Record")
        link = get_path(rec, self.cfg.get("url_field")) if self.cfg.get("url_field") else None
        when = get_path(rec, self.cfg.get("date_field")) if self.cfg.get("date_field") else None
        key = (
            str(rid)
            if rid is not None
            else hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()[:16]
        )
        hints: dict[str, Any] = {"title": title}
        if when and (iso := to_iso(str(when))):
            hints["date"] = iso
        if link and self.cfg.get("download_url_field"):
            target = urljoin(self.cfg["url"], str(link))

            def fetch() -> bytes:
                rr = self.http.get(target)
                rr.raise_for_status()
                return check_max_bytes(rr.content, self.cfg, target)

            return Item(
                key=key,
                filename=filename_for(target, None, "record"),
                fetch=fetch,
                url=target,
                title=title,
                hints=hints,
            )
        only = [f.strip() for f in (self.cfg.get("text_fields") or "").split(",") if f.strip()] or None
        text = render_record(rec, title, {self.cfg.get("title_field") or "title"}, only)
        if link:
            text += f"\n\nSource: {link}"
        data = text.encode("utf-8")
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", key)[:60] or "record"
        return Item(
            key=key,
            filename=f"record-{safe}.md",
            fetch=lambda: data,
            url=str(link) if link else None,
            title=title,
            hints=hints,
            size=len(data),
        )


def _link_next(header: str) -> str | None:
    m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', header)
    return m.group(1) if m else None
