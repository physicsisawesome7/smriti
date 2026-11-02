"""Tools agents can call.

Every tool result becomes a numbered, citable passage, so an agent's final
answer goes through the same sentence-by-sentence verification as a plain
question: a number that came from the calculator, a SQL row or an API response
must appear in that result, or the sentence fails.

Built-in tools need no setup. Configured tools (REST APIs, MCP servers,
databases, a web-search provider) are saved by a curator, with credentials
sealed at rest and a list of roles that may use them.

Each action declares whether it changes something (``mutates``). The runtime
asks the person to approve those before they run.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import math
import operator
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx

from ..connectors.base import F
from ..egress import EgressDenied, check_url, guarded_client
from ..guardrails import injection_score
from ..models import Passage, Principal
from . import sandbox

MAX_RESULT_CHARS = 6000
NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


class ToolError(RuntimeError):
    pass


@dataclass
class Action:
    name: str
    description: str
    params: dict[str, Any]
    mutates: bool = False
    meta: dict[str, Any] = field(default_factory=dict)  # e.g. method/path for REST

    def spec(self, fn_name: str) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": fn_name, "description": self.description[:1000], "parameters": self.params},
        }


@dataclass
class ToolResult:
    text: str
    passages: list[Passage] = field(default_factory=list)  # citable evidence; numbered by the runtime
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    untrusted: bool = False  # content from outside Smriti's governed library


@dataclass
class ToolCtx:
    tenant: Any
    principal: Principal
    config: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] = field(default_factory=dict)
    actions_cache: list[dict[str, Any]] = field(default_factory=list)
    scope: list[str] | None = None  # the agent's documents
    conversation_id: str | None = None
    tool_id: int | None = None
    name: str = ""


def params(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or list(props)}


def S(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def evidence(
    tool: str, title: str, text: str, *, url: str | None = None, section: str = "", doc_class: str = "internal"
) -> Passage:
    text = text.strip()[:MAX_RESULT_CHARS]
    key = hashlib.sha256(f"{tool}|{url}|{text}".encode()).hexdigest()[:24]
    return Passage(
        n=0,
        chunk_id=key,
        doc_id=f"{tool}:{url or key[:12]}",
        version=1,
        title=title[:200],
        ref_no=None,
        doc_date=time.strftime("%Y-%m-%d"),
        doc_type="tool-result",
        clause_id=None,
        section=section,
        page=1,
        text=text,
        score=1.0,
        status="current",
        doc_class=doc_class,
        note=url,
        injection_flag=injection_score(text)[0] >= 0.5,
    )


class Tool:
    kind: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    icon: ClassVar[str] = "◇"
    fields: ClassVar[list[dict[str, Any]]] = []
    configurable: ClassVar[bool] = False  # needs a saved configuration
    external: ClassVar[bool] = False  # sends the agent's words outside this deployment

    def __init__(self, ctx: ToolCtx):
        self.ctx = ctx
        self.cfg = {f["name"]: f.get("default") for f in self.fields}
        self.cfg.update({k: v for k, v in ctx.config.items() if v not in (None, "")})

    @classmethod
    def availability(cls) -> tuple[bool, str]:
        return True, ""

    def actions(self) -> list[Action]:
        raise NotImplementedError

    def discover(self) -> list[dict[str, Any]]:
        """For configured tools: read the actions from the target (OpenAPI spec, MCP list)."""
        return []

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        raise NotImplementedError


# ---------------------------------------------------------------- built-in


class KnowledgeSearch(Tool):
    kind = "search"
    label = "Search the library"
    description = "Permission-aware search over the documents this agent may use, with version rules applied."
    icon = "◎"

    def actions(self) -> list[Action]:
        return [
            Action(
                "search_documents",
                "Search the organisation's documents. Returns numbered passages to cite. Use specific terms, clause numbers or reference numbers.",
                {
                    "type": "object",
                    "properties": {
                        "query": S("what to look for"),
                        "as_of": S("optional date YYYY-MM-DD: use the version in force on that date"),
                    },
                    "required": ["query"],
                },
            )
        ]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        t, p = self.ctx.tenant, self.ctx.principal
        q = str(args.get("query") or "").strip()
        if not q:
            raise ToolError("query is empty")
        scope = self.ctx.scope
        passages, _ = t.retrieve(
            p,
            q,
            as_of=args.get("as_of") or None,
            top_k=6,
            doc_ids=None if scope is None else (scope or ["~no-documents~"]),
        )
        if not passages:
            return ToolResult("No documents you can access match this search.")
        return ToolResult("", passages=passages)


class Calculator(Tool):
    kind = "calculator"
    label = "Calculator"
    description = "Exact arithmetic, so totals, percentages and dates are computed, not guessed."
    icon = "∑"
    OPS: ClassVar[dict[type, Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }
    FUNCS: ClassVar[dict[str, Any]] = {
        "round": round,
        "min": min,
        "max": max,
        "abs": abs,
        "sum": lambda *a: sum(a[0]) if len(a) == 1 and isinstance(a[0], list) else sum(a),
        "sqrt": math.sqrt,
        "ceil": math.ceil,
        "floor": math.floor,
    }

    def actions(self) -> list[Action]:
        return [
            Action(
                "calculate",
                "Evaluate an arithmetic expression, e.g. (1250000 * 0.05) + 3000. Supports + - * / // % **, round, min, max, sum, abs, sqrt.",
                params({"expression": S("the expression")}),
            )
        ]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        expr = str(args.get("expression") or "").replace(",", "").replace("₹", "").strip()
        try:
            value = self._eval(ast.parse(expr, mode="eval").body)
        except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as e:
            raise ToolError(f"cannot evaluate {expr!r}: {e}") from e
        if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
            value = int(value)
        shown = (
            (_lakh(value) + " in Indian grouping, " + f"{value:,}")
            if isinstance(value, int) and abs(value) >= 100000
            else f"{value:,}"
            if isinstance(value, int)
            else f"{value:,.6g}"
            if isinstance(value, float)
            else str(value)
        )
        text = f"{expr} = {value} ({shown})"
        return ToolResult(text, passages=[evidence("calc", "Calculator", text)])

    def _eval(self, n: ast.AST) -> Any:
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in self.OPS:
            a, b = self._eval(n.left), self._eval(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 100:
                raise ValueError("exponent too large")
            return self.OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in self.OPS:
            return self.OPS[type(n.op)](self._eval(n.operand))
        if isinstance(n, ast.List):
            return [self._eval(x) for x in n.elts]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in self.FUNCS and not n.keywords:
            return self.FUNCS[n.func.id](*[self._eval(a) for a in n.args])
        raise ValueError("only numbers, arithmetic and round/min/max/sum/abs/sqrt are allowed")


def _lakh(n: int) -> str:
    """12345678 -> 1,23,45,678 (lakh and crore grouping)."""
    s = str(abs(n))
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return ("-" if n < 0 else "") + (head + "," + tail if head else tail)


class ReadUrl(Tool):
    kind = "fetch"
    label = "Read a web page"
    description = "Opens one URL (web page or document) and reads its text. Every request passes the egress guard."
    icon = "↗"
    external = True

    def actions(self) -> list[Action]:
        return [
            Action("read_url", "Read the text of a web page or online document.", params({"url": S("http(s) URL")}))
        ]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        from ..parsers import parse

        url = str(args.get("url") or "").strip()
        try:
            check_url(url)
            with guarded_client(timeout=20, follow_redirects=True, headers={"User-Agent": "SmritiAgent/1.0"}) as c:
                r = c.get(url)
                r.raise_for_status()
        except (EgressDenied, httpx.HTTPError) as e:
            raise ToolError(f"cannot read {url}: {e}") from e
        data = r.content[: 10 * 2**20]
        ctype = r.headers.get("content-type", "").split(";")[0]
        name = urlsplit(url).path.rsplit("/", 1)[-1] or "page"
        if "html" in ctype or "." not in name:
            name = name.split(".")[0] + ".html" if "html" in ctype else name + ".txt"
        doc = parse(data, name)
        return ToolResult(
            "", passages=[evidence("web", doc.title or url, doc.text, url=url, doc_class="public")], untrusted=True
        )


class WebSearch(Tool):
    kind = "web_search"
    label = "Web search"
    description = "Searches the web through self-hosted SearxNG (default), Brave or Tavily. Personal data in the query is masked first."
    icon = "⌕"
    external = True
    fields: ClassVar[list[dict[str, Any]]] = [
        F("provider", "Provider", "select", default="searxng", options=["searxng", "brave", "tavily"]),
        F("url", "SearxNG URL", "url", placeholder="http://searxng:8080", help="searxng only"),
        F("api_key", "API key", "secret", help="Brave or Tavily; stored encrypted"),
        F("max_results", "Results per search", "number", default=5),
    ]

    @classmethod
    def availability(cls) -> tuple[bool, str]:
        if os.environ.get("SMRITI_SEARXNG_URL"):
            return True, ""
        return False, "set SMRITI_SEARXNG_URL (compose profile `search`) or add a web-search tool with a provider key"

    def actions(self) -> list[Action]:
        return [
            Action(
                "web_search",
                "Search the public web. Returns numbered results to cite.",
                params({"query": S("search terms")}),
            )
        ]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        q = str(args.get("query") or "").strip()
        n = int(self.cfg.get("max_results") or 5)
        provider = self.cfg.get("provider") or "searxng"
        key = self.ctx.secrets.get("api_key")
        try:
            if provider == "brave":
                with guarded_client(timeout=15) as c:
                    r = c.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": q, "count": n},
                        headers={"X-Subscription-Token": key or "", "Accept": "application/json"},
                    )
                    r.raise_for_status()
                    hits = [
                        (x.get("title", ""), x.get("url", ""), x.get("description", ""))
                        for x in r.json().get("web", {}).get("results", [])
                    ]
            elif provider == "tavily":
                with guarded_client(timeout=20) as c:
                    r = c.post("https://api.tavily.com/search", json={"api_key": key, "query": q, "max_results": n})
                    r.raise_for_status()
                    hits = [
                        (x.get("title", ""), x.get("url", ""), x.get("content", ""))
                        for x in r.json().get("results", [])
                    ]
            else:
                base = (self.cfg.get("url") or os.environ.get("SMRITI_SEARXNG_URL") or "").rstrip("/")
                if not base:
                    raise ToolError("no SearxNG URL configured")
                # the operator's own SearxNG on the internal network; user-entered URLs go through the guard
                client = httpx.Client(timeout=15) if not self.cfg.get("url") else guarded_client(timeout=15)
                with client as c:
                    r = c.get(f"{base}/search", params={"q": q, "format": "json"})
                    r.raise_for_status()
                    hits = [
                        (x.get("title", ""), x.get("url", ""), x.get("content", ""))
                        for x in r.json().get("results", [])
                    ]
        except (EgressDenied, httpx.HTTPError, ValueError) as e:
            raise ToolError(f"web search failed: {e}") from e
        hits = [h for h in hits if h[1]][:n]
        if not hits:
            return ToolResult("No web results.", untrusted=True)
        return ToolResult(
            "",
            passages=[evidence("web", t or u, s or t, url=u, doc_class="public") for t, u, s in hits],
            untrusted=True,
        )


class CodeRunner(Tool):
    kind = "code"
    label = "Run Python"
    description = "Runs Python in an isolated sandbox with no network, for analysis, charts and file conversion. Files it writes are returned."
    icon = "⌘"

    @classmethod
    def availability(cls) -> tuple[bool, str]:
        m = sandbox.mode()
        return (m != "off"), (
            "" if m != "off" else "no sandbox: set SMRITI_SANDBOX_URL (the Docker stack runs one by default)"
        )

    def actions(self) -> list[Action]:
        return [
            Action(
                "run_python",
                "Run a Python 3 script and get stdout. No network. Write files to the current directory to return them (e.g. a CSV or PNG chart).",
                params({"code": S("the full script")}),
            )
        ]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        try:
            out = sandbox.run(str(args.get("code") or ""), timeout_s=20)
        except sandbox.SandboxUnavailable as e:
            raise ToolError(str(e)) from e
        arts = [save_artifact(self.ctx, f["name"], base64.b64decode(f["b64"])) for f in out.get("files", [])]
        text = f"exit code {out.get('exit_code')}" + (" (timed out)" if out.get("timed_out") else "")
        if out.get("stdout"):
            text += f"\nstdout:\n{out['stdout'][-4000:]}"
        if out.get("stderr"):
            text += f"\nstderr:\n{out['stderr'][-1500:]}"
        if arts:
            text += "\nfiles: " + ", ".join(a["filename"] for a in arts)
        return ToolResult(text, passages=[evidence("code", "Python sandbox output", text)], artifacts=arts)


class CreateFile(Tool):
    kind = "artifact"
    label = "Create a file"
    description = "Saves a document the agent writes (Markdown, CSV, JSON, HTML or text) for the person to download."
    icon = "▣"
    MIMES: ClassVar[dict[str, str]] = {
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".json": "application/json",
        ".html": "text/html",
        ".txt": "text/plain",
    }

    def actions(self) -> list[Action]:
        return [
            Action(
                "create_file",
                "Save a file for the person: a memo draft, a CSV table, a JSON export. Returns its id.",
                params({"filename": S("e.g. memo.md or totals.csv"), "content": S("the full file content")}),
            )
        ]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        name = re.sub(r"[^\w.\- ]+", "_", str(args.get("filename") or "file.md"))[:120]
        if not any(name.lower().endswith(x) for x in self.MIMES):
            name += ".md"
        a = save_artifact(self.ctx, name, str(args.get("content") or "").encode("utf-8"))
        return ToolResult(f"Saved {a['filename']} ({a['size']} bytes) for the person to download.", artifacts=[a])


class Skills(Tool):
    kind = "skills"
    label = "Tenant skills"
    description = (
        "The tenant's coded skills (e.g. tender validation, circular impact) as tools, with their own grant checks."
    )
    icon = "✓"

    def actions(self) -> list[Action]:
        out = []
        for s in self.ctx.tenant.pack.skills.values():
            if not all(self.ctx.principal.can(g) for g in s.meta.get("requires", [])):
                continue
            inputs = s.meta.get("inputs", {}) or {}
            props = {k: S(str((v or {}).get("label", k))) for k, v in inputs.items()}
            out.append(
                Action("skill_" + NAME_RE.sub("_", s.name), s.description, params(props), meta={"skill": s.name})
            )
        return out

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        from ..skills import run_skill

        a = next((x for x in self.actions() if x.name == action), None)
        if not a:
            raise ToolError(f"unknown skill {action}")
        r = run_skill(self.ctx.tenant, self.ctx.principal, a.meta["skill"], {k: str(v) for k, v in args.items()})
        lines = [f"Skill {a.meta['skill']}: " + ", ".join(f"{v} {k}" for k, v in r["summary"].items() if v)]
        lines += [f"- {f['severity']}: {f.get('message') or f.get('title') or ''}" for f in r["findings"][:12]]
        if r.get("draft"):
            lines.append(f"A draft was created for review (draft #{r['draft']['id']}).")
        if r.get("citations"):
            lines.append(
                "Sources: "
                + "; ".join(c["title"] + (f" ({c['ref_no']})" if c.get("ref_no") else "") for c in r["citations"][:8])
            )
        text = "\n".join(lines)
        return ToolResult(text, passages=[evidence("skill", f"Skill: {a.meta['skill']}", text)])


# ---------------------------------------------------------------- configured


ACCESS_FIELDS = [
    F(
        "allowed_roles",
        "Roles that may use it (comma separated)",
        group="Access",
        help="empty = anyone who can use the agent",
    ),
    F(
        "doc_class",
        "Sensitivity of its results",
        "select",
        default="internal",
        options=["public", "internal", "confidential", "secret"],
        group="Access",
    ),
]


class RestApi(Tool):
    kind = "rest_api"
    label = "REST API"
    description = (
        "Any HTTP API. Import its operations from an OpenAPI spec or list them by hand. Write operations need approval."
    )
    icon = "⇄"
    configurable = True
    fields: ClassVar[list[dict[str, Any]]] = [
        F("base_url", "Base URL", "url", required=True, placeholder="https://erp.internal.example/api"),
        F("openapi_url", "OpenAPI spec URL", "url", help="optional; imports every operation"),
        F(
            "operations",
            "Operations (JSON)",
            "json",
            placeholder='[{"name": "get_contract", "method": "GET", "path": "/contracts/{id}", "description": "Contract by id"}]',
            help="used when there is no spec",
        ),
        F("auth", "Authentication", "select", default="none", options=["none", "bearer", "header", "basic", "query"]),
        F("auth_name", "Header or query parameter name", placeholder="X-API-Key"),
        F("token", "Token, key or user:password", "secret", help="stored encrypted"),
        F("allow_writes", "Expose write operations (POST, PUT, PATCH, DELETE)", "bool", default=False),
        *ACCESS_FIELDS,
    ]

    def _client(self) -> httpx.Client:
        headers = {"Accept": "application/json", "User-Agent": "SmritiAgent/1.0"}
        auth = None
        tok = self.ctx.secrets.get("token") or ""
        mode = self.cfg.get("auth") or "none"
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {tok}"
        elif mode == "header":
            headers[self.cfg.get("auth_name") or "X-API-Key"] = tok
        elif mode == "basic" and ":" in tok:
            auth = tuple(tok.split(":", 1))
        return guarded_client(timeout=30, follow_redirects=True, headers=headers, auth=auth)

    def discover(self) -> list[dict[str, Any]]:
        allow = bool(self.cfg.get("allow_writes"))
        if self.cfg.get("openapi_url"):
            try:
                with self._client() as c:
                    r = c.get(self.cfg["openapi_url"])
                    r.raise_for_status()
                    text = r.text
                spec = json.loads(text) if text.lstrip().startswith("{") else _yaml(text)
            except (EgressDenied, httpx.HTTPError, ValueError) as e:
                raise ToolError(f"cannot read the OpenAPI spec: {e}") from e
            ops = openapi_actions(spec, allow)
        else:
            raw = self.cfg.get("operations") or []
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw or "[]")
                except ValueError as e:
                    raise ToolError(f"operations is not valid JSON: {e}") from e
            ops = []
            for o in raw:
                method = str(o.get("method", "GET")).upper()
                if method != "GET" and not allow:
                    continue
                path = str(o.get("path", "/"))
                props = {k: S(f"path parameter {k}") for k in re.findall(r"\{(\w+)\}", path)}
                schema = o.get("params") or params(props, list(props))
                ops.append(
                    {
                        "name": NAME_RE.sub("_", str(o.get("name") or f"{method}_{path}")).strip("_")[:60],
                        "description": str(o.get("description") or f"{method} {path}"),
                        "params": schema,
                        "mutates": method not in ("GET", "HEAD"),
                        "meta": {"method": method, "path": path, "in": {k: "path" for k in props}},
                    }
                )
        if not ops:
            raise ToolError("no operations found")
        return ops[:40]

    def actions(self) -> list[Action]:
        return [Action(**a) for a in self.ctx.actions_cache]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        a = next((x for x in self.actions() if x.name == action), None)
        if not a:
            raise ToolError(f"unknown operation {action}")
        method, path, where = a.meta["method"], a.meta["path"], a.meta.get("in", {})
        args = dict(args)
        for k in re.findall(r"\{(\w+)\}", path):
            if k not in args:
                raise ToolError(f"missing path parameter {k}")
            path = path.replace("{" + k + "}", quote(str(args.pop(k)), safe=""))
        query = {k: v for k, v in args.items() if where.get(k) == "query" or (method == "GET" and k not in where)}
        headers = {k: str(v) for k, v in args.items() if where.get(k) == "header"}
        body = (
            args.get("body")
            if "body" in args
            else {k: v for k, v in args.items() if k not in query and k not in headers}
        )
        if (self.cfg.get("auth") or "") == "query":
            query[self.cfg.get("auth_name") or "api_key"] = self.ctx.secrets.get("token") or ""
        url = self.cfg["base_url"].rstrip("/") + path
        try:
            with self._client() as c:
                r = c.request(
                    method,
                    url,
                    params=query,
                    headers=headers,
                    json=body if method not in ("GET", "HEAD") and body else None,
                )
        except (EgressDenied, httpx.HTTPError) as e:
            raise ToolError(f"{method} {path} failed: {e}") from e
        try:
            shown = json.dumps(r.json(), ensure_ascii=False, indent=1)
        except ValueError:
            shown = r.text
        text = f"HTTP {r.status_code} {method} {path}\n{shown[:MAX_RESULT_CHARS]}"
        return ToolResult(
            text,
            passages=[
                evidence(
                    "api",
                    f"{self.ctx.name}: {method} {a.meta['path']}",
                    text,
                    url=url.split("?")[0],
                    doc_class=self.cfg.get("doc_class") or "internal",
                )
            ],
            untrusted=True,
        )


def _yaml(text: str) -> dict[str, Any]:
    import yaml

    out = yaml.safe_load(text)
    if not isinstance(out, dict):
        raise ValueError("spec is not a mapping")
    return out


def openapi_actions(spec: dict[str, Any], allow_writes: bool) -> list[dict[str, Any]]:
    def deref(x: Any, depth: int = 0) -> Any:
        if isinstance(x, dict) and "$ref" in x and depth < 4:
            node: Any = spec
            for part in str(x["$ref"]).lstrip("#/").split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            return deref(node, depth + 1)
        return x

    out = []
    for path, item in (spec.get("paths") or {}).items():
        shared = [deref(p) for p in item.get("parameters", [])]
        for method, op in item.items():
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "PATCH", "DELETE") or not isinstance(op, dict):
                continue
            if m != "GET" and not allow_writes:
                continue
            props: dict[str, Any] = {}
            req: list[str] = []
            where: dict[str, str] = {}
            for prm in shared + [deref(p) for p in op.get("parameters", [])]:
                if not prm.get("name") or prm.get("in") == "cookie":
                    continue
                sch = deref(prm.get("schema") or {"type": "string"})
                props[prm["name"]] = {
                    **{k: v for k, v in sch.items() if k in ("type", "enum", "format")},
                    "description": prm.get("description", "")[:200],
                }
                where[prm["name"]] = prm.get("in", "query")
                if prm.get("required"):
                    req.append(prm["name"])
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            if body:
                props["body"] = _plain(deref(body), deref)
                where["body"] = "body"
                if (op.get("requestBody") or {}).get("required"):
                    req.append("body")
            name = op.get("operationId") or f"{m.lower()}_{path}"
            out.append(
                {
                    "name": NAME_RE.sub("_", name).strip("_")[:60],
                    "description": (op.get("summary") or op.get("description") or f"{m} {path}")[:500],
                    "params": {"type": "object", "properties": props, "required": req},
                    "mutates": m != "GET",
                    "meta": {"method": m, "path": path, "in": where},
                }
            )
    return out


def _plain(schema: Any, deref: Any, depth: int = 0) -> Any:
    """Inline $refs a few levels deep so the model sees a self-contained schema."""
    schema = deref(schema)
    if depth > 3 or not isinstance(schema, dict):
        return {"type": "object"} if depth > 3 else schema
    out = {k: v for k, v in schema.items() if k in ("type", "description", "enum", "required", "format")}
    if "properties" in schema:
        out["properties"] = {k: _plain(v, deref, depth + 1) for k, v in schema["properties"].items()}
    if "items" in schema:
        out["items"] = _plain(schema["items"], deref, depth + 1)
    return out or {"type": "object"}


class McpTools(Tool):
    kind = "mcp"
    label = "MCP server"
    description = "Every tool of a Model Context Protocol server over HTTP. Tools not marked read-only need approval."
    icon = "⌬"
    configurable = True
    fields: ClassVar[list[dict[str, Any]]] = [
        F("url", "Server URL", "url", required=True, placeholder="https://mcp.internal.example/mcp"),
        F("transport", "Transport", "select", default="streamable-http", options=["streamable-http", "sse"]),
        F("token", "Bearer token", "secret", help="optional; stored encrypted"),
        F("only", "Only tools matching (regex)", placeholder="^(search|get)_"),
        *ACCESS_FIELDS,
    ]

    async def _session(self, fn: Any) -> Any:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        url = self.cfg["url"].strip()
        check_url(url)
        headers = {"Authorization": f"Bearer {self.ctx.secrets['token']}"} if self.ctx.secrets.get("token") else {}
        if self.cfg.get("transport") == "sse":
            transport = sse_client(url, headers=headers, timeout=60)
        else:
            transport = streamable_http_client(url, http_client=create_mcp_http_client(headers=headers))
        async with transport as streams, ClientSession(streams[0], streams[1], read_timeout_seconds=60) as s:
            await s.initialize()
            return await fn(s)

    def _run(self, fn: Any) -> Any:
        try:
            return asyncio.run(self._session(fn))
        except EgressDenied as e:
            raise ToolError(str(e)) from e
        except ImportError as e:
            raise ToolError("MCP tools need `pip install smriti[mcp]`") from e
        except Exception as e:
            from ..connectors.mcp_source import _root_cause

            raise ToolError(f"MCP server error: {_root_cause(e)}") from e

    def discover(self) -> list[dict[str, Any]]:
        async def lst(s: Any) -> list[Any]:
            return list((await s.list_tools()).tools)

        pat = re.compile(self.cfg["only"]) if self.cfg.get("only") else None
        out = []
        for tl in self._run(lst):
            if pat and not pat.search(tl.name):
                continue
            ann = getattr(tl, "annotations", None)
            ro = bool(getattr(ann, "read_only_hint", None) or getattr(ann, "readOnlyHint", None))
            schema = (
                getattr(tl, "input_schema", None)
                or getattr(tl, "inputSchema", None)
                or {"type": "object", "properties": {}}
            )
            out.append(
                {
                    "name": NAME_RE.sub("_", tl.name)[:60],
                    "description": (tl.description or tl.name)[:500],
                    "params": schema,
                    "mutates": not ro,  # the safe default: unknown tools may change things
                    "meta": {"tool": tl.name},
                }
            )
        if not out:
            raise ToolError("the server lists no tools")
        return out[:40]

    def actions(self) -> list[Action]:
        return [Action(**a) for a in self.ctx.actions_cache]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        a = next((x for x in self.actions() if x.name == action), None)
        if not a:
            raise ToolError(f"unknown MCP tool {action}")

        async def go(s: Any) -> Any:
            return await s.call_tool(a.meta["tool"], args)

        res = self._run(go)
        parts = []
        for c in getattr(res, "content", []) or []:
            if getattr(c, "text", None) is not None:
                parts.append(c.text)
            else:
                parts.append(f"<{getattr(c, 'type', 'content')}>")
        if getattr(res, "structured_content", None):
            parts.append(json.dumps(res.structured_content, ensure_ascii=False)[:MAX_RESULT_CHARS])
        text = ("ERROR: " if getattr(res, "is_error", False) else "") + "\n".join(parts)
        return ToolResult(
            text,
            passages=[
                evidence(
                    "mcp", f"{self.ctx.name}: {a.meta['tool']}", text, doc_class=self.cfg.get("doc_class") or "internal"
                )
            ],
            untrusted=True,
        )


class SqlQuery(Tool):
    kind = "sql"
    label = "Database (read-only SQL)"
    description = (
        "Lets the agent run read-only SELECT queries on one database, with row caps and a read-only transaction."
    )
    icon = "🗄"
    configurable = True
    fields: ClassVar[list[dict[str, Any]]] = [
        F(
            "url",
            "Connection URL",
            "secret",
            required=True,
            help="SQLAlchemy URL of a read-only account; stored encrypted",
        ),
        F(
            "schema_notes",
            "Tables and columns the agent should know",
            "textarea",
            placeholder="contracts(id, title, value_inr, signed_on)",
        ),
        F("max_rows", "Max rows per query", "number", default=200),
        *ACCESS_FIELDS,
    ]

    def actions(self) -> list[Action]:
        notes = (self.cfg.get("schema_notes") or "").strip()
        return [
            Action(
                "run_sql",
                "Run one read-only SQL SELECT on the database." + (f" Schema: {notes[:1500]}" if notes else ""),
                params({"query": S("a single SELECT or WITH statement")}),
            )
        ]

    def call(self, action: str, args: dict[str, Any]) -> ToolResult:
        from sqlalchemy import text

        from ..connectors.base import ConnectorError, Ctx
        from ..connectors.sql import DatabaseConnector, _cell, check_query

        try:
            q = check_query(str(args.get("query") or ""))
            eng = DatabaseConnector(
                Ctx(config={}, secrets={"url": self.ctx.secrets.get("url", "")}, state={})
            )._engine()
        except ConnectorError as e:
            raise ToolError(str(e)) from e
        limit = int(self.cfg.get("max_rows") or 200)
        try:
            with eng.connect() as conn:
                if eng.dialect.name == "postgresql":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    conn.exec_driver_sql("SET statement_timeout = 30000")
                elif eng.dialect.name in ("mysql", "mariadb"):
                    conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                res = conn.execute(text(q))
                cols = list(res.keys())
                rows = res.fetchmany(limit + 1)
                conn.rollback()
        except Exception as e:
            raise ToolError(f"query failed: {str(e).splitlines()[0][:300]}") from e
        finally:
            eng.dispose()
        more = len(rows) > limit
        rows = rows[:limit]
        md = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
        md += "\n".join("| " + " | ".join(_cell(v).replace("|", "/") for v in r) + " |" for r in rows)
        text_ = f"{len(rows)} row(s){' (truncated)' if more else ''}\n{md}"
        return ToolResult(
            text_,
            passages=[
                evidence(
                    "sql",
                    f"{self.ctx.name}: query result",
                    text_,
                    section=q[:200],
                    doc_class=self.cfg.get("doc_class") or "internal",
                )
            ],
            untrusted=True,
        )


class ConfiguredWebSearch(WebSearch):
    kind = "web_search_provider"
    label = "Web search (provider)"
    description = "Web search through SearxNG at a URL you choose, or Brave or Tavily with your API key."
    configurable = True

    @classmethod
    def availability(cls) -> tuple[bool, str]:
        return True, ""


BUILTIN: dict[str, type[Tool]] = {
    c.kind: c for c in (KnowledgeSearch, Calculator, ReadUrl, WebSearch, CodeRunner, CreateFile, Skills)
}
CONFIGURABLE: dict[str, type[Tool]] = {c.kind: c for c in (RestApi, McpTools, SqlQuery, ConfiguredWebSearch)}


def catalog() -> dict[str, Any]:
    def card(c: type[Tool]) -> dict[str, Any]:
        ok, why = c.availability()
        return {
            "kind": c.kind,
            "label": c.label,
            "description": c.description,
            "icon": c.icon,
            "available": ok,
            "why_unavailable": why,
            "external": c.external,
            "fields": c.fields if c.configurable else [],
        }

    return {"builtin": [card(c) for c in BUILTIN.values()], "configurable": [card(c) for c in CONFIGURABLE.values()]}


def save_artifact(ctx: ToolCtx, filename: str, data: bytes) -> dict[str, Any]:
    from ..store import artifacts

    t = ctx.tenant
    ext = os.path.splitext(filename)[1].lower()
    mime = {
        **CreateFile.MIMES,
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(ext, "application/octet-stream")
    aid = uuid.uuid4().hex
    key = t.blobs.put(data, ext)
    t.store.insert(
        artifacts,
        id=aid,
        tenant=t.id,
        user_id=ctx.principal.user_id,
        conversation_id=ctx.conversation_id,
        filename=filename,
        mime=mime,
        blob_key=key,
        size=len(data),
        created_at=time.time(),
    )
    return {"id": aid, "filename": filename, "mime": mime, "size": len(data)}
