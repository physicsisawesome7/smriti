"""MCP connector: ingest from any Model Context Protocol server over HTTP.

Two modes:

* **resources**: list the server's resources (following pagination), read
  each one, and ingest its contents: text as Markdown or plain text, binary
  blobs (PDFs, Office files) as files;
* **tool**: call one tool with fixed arguments and ingest what it returns,
  e.g. `search(query="circulars 2026")` on a document-management server.

Only network transports (streamable HTTP, and legacy SSE) are offered; a
stdio server would mean running an arbitrary command on this host, so it is
not configurable from the UI. Requires `pip install smriti[mcp]` (MCP SDK 2.x).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any

from ..egress import EgressDenied, check_url
from .base import COMMON_FIELDS, EXT_BY_MIME, Connector, ConnectorError, F, Item


class McpConnector(Connector):
    kind = "mcp"
    label = "MCP server"
    description = "Connects to a Model Context Protocol server over HTTP and ingests its resources, or the result of one tool call."
    icon = "⌬"
    fields = [
        F(
            "url",
            "Server URL",
            "url",
            required=True,
            placeholder="https://mcp.internal.example/mcp",
            help="streamable-HTTP endpoint (usually ends in /mcp)",
        ),
        F("transport", "Transport", "select", default="streamable-http", options=["streamable-http", "sse"]),
        F("token", "Bearer token", "secret", help="optional; stored encrypted"),
        F("mode", "Ingest", "select", default="resources", options=["resources", "tool"], group="Selection"),
        F(
            "uri_filter",
            "Only resources whose URI matches (regex)",
            group="Selection",
            placeholder="^notes://circulars/",
        ),
        F("tool_name", "Tool name", group="Selection", help="tool mode"),
        F(
            "tool_args",
            "Tool arguments (JSON)",
            "json",
            group="Selection",
            placeholder='{"query": "performance guarantee"}',
        ),
        F("timeout_s", "Timeout per call (s)", "number", default=60, group="Selection"),
        *COMMON_FIELDS,
    ]

    def discover(self) -> Iterator[Item]:
        url = self.cfg["url"].strip()
        try:
            check_url(url)
        except EgressDenied as e:
            raise ConnectorError(str(e)) from e
        try:
            items = asyncio.run(self._collect(url))
        except ConnectorError:
            raise
        except ImportError as e:
            raise ConnectorError("the MCP connector needs `pip install smriti[mcp]`") from e
        except Exception as e:
            raise ConnectorError(f"MCP server error: {_root_cause(e)}") from e
        yield from items

    async def _collect(self, url: str) -> list[Item]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        headers = {"Authorization": f"Bearer {self.secrets['token']}"} if self.secrets.get("token") else {}
        timeout = float(self.cfg.get("timeout_s") or 60)
        if self.cfg.get("transport") == "sse":
            transport = sse_client(url, headers=headers, timeout=timeout)
        else:
            transport = streamable_http_client(url, http_client=create_mcp_http_client(headers=headers))
        out: list[Item] = []
        async with transport as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                await session.initialize()
                if (self.cfg.get("mode") or "resources") == "tool":
                    out += await self._tool(session)
                else:
                    out += await self._resources(session)
        return out

    async def _resources(self, session: Any) -> list[Item]:
        from mcp import types

        pattern = re.compile(self.cfg["uri_filter"]) if self.cfg.get("uri_filter") else None
        limit = int(self.cfg.get("max_items") or 500)
        resources: list[Any] = []
        cursor = None
        while len(resources) < limit:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_resources(params=params)
            resources += [r for r in page.resources if not pattern or pattern.search(str(r.uri))]
            cursor = getattr(page, "next_cursor", None)
            if not cursor or self.ctx.cancelled():
                break
        out: list[Item] = []
        for res in resources[:limit]:
            if self.ctx.cancelled():
                break
            uri = str(res.uri)
            result = await session.read_resource(uri)
            for i, c in enumerate(getattr(result, "contents", []) or []):
                mime = (getattr(c, "mime_type", None) or getattr(res, "mime_type", None) or "").split(";")[0]
                if getattr(c, "text", None) is not None:
                    data = c.text.encode("utf-8")
                    ext = ".md" if mime in ("", "text/markdown") else EXT_BY_MIME.get(mime, ".txt")
                elif getattr(c, "blob", None) is not None:
                    data = base64.b64decode(c.blob)
                    ext = EXT_BY_MIME.get(mime, ".bin")
                else:
                    continue
                name = getattr(res, "title", None) or res.name or uri
                key = uri if i == 0 else f"{uri}#{i}"
                stem = re.sub(r"[^A-Za-z0-9_-]+", "-", str(res.name or uri))[:60].strip("-") or "resource"
                out.append(
                    Item(
                        key=key,
                        filename=stem + ext,
                        fetch=lambda d=data: d,
                        url=uri,
                        title=str(name),
                        hints={"title": str(name)},
                        size=len(data),
                    )
                )
        return out

    async def _tool(self, session: Any) -> list[Item]:
        name = self.cfg.get("tool_name")
        if not name:
            raise ConnectorError("tool mode needs a tool name")
        args = self.cfg.get("tool_args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError as e:
                raise ConnectorError(f"tool arguments: invalid JSON ({e})") from e
        result = await session.call_tool(name, args)
        if getattr(result, "is_error", False):
            texts = [getattr(c, "text", "") for c in getattr(result, "content", [])]
            raise ConnectorError(f"tool '{name}' failed: {' '.join(texts)[:300]}")
        parts: list[str] = []
        out: list[Item] = []
        for c in getattr(result, "content", []) or []:
            if getattr(c, "text", None):
                parts.append(c.text)
            elif getattr(c, "resource", None) is not None:
                r = c.resource
                if getattr(r, "text", None):
                    parts.append(r.text)
                elif getattr(r, "blob", None):
                    data = base64.b64decode(r.blob)
                    ext = EXT_BY_MIME.get((getattr(r, "mime_type", "") or "").split(";")[0], ".bin")
                    out.append(
                        Item(
                            key=str(r.uri),
                            filename=f"tool-result{len(out)}{ext}",
                            fetch=lambda d=data: d,
                            url=str(r.uri),
                        )
                    )
        if getattr(result, "structured_content", None) and not parts:
            parts.append(json.dumps(result.structured_content, ensure_ascii=False, indent=1))
        if parts:
            title = f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})"[:200]
            data = (f"# {title}\n\n" + "\n\n".join(parts)).encode("utf-8")
            key = "tool:" + hashlib.sha256(f"{name}|{json.dumps(args, sort_keys=True)}".encode()).hexdigest()[:16]
            out.append(
                Item(
                    key=key,
                    filename="tool-result.md",
                    fetch=lambda: data,
                    title=title,
                    hints={"title": title},
                    size=len(data),
                )
            )
        return out


def _root_cause(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {str(e)[:300]}"
