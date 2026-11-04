"""Database connector: one read-only SQL query.

Two modes:

* **rows**: every row becomes a document (title, date and text columns
  mapped), versioned by its id, so an updated row becomes a new version;
* **table**: the whole result becomes one table document, so totals and
  counts over it are computed in code from the rows.

Safety: the query must be a single SELECT (or WITH ... SELECT); data-changing
keywords are refused; the transaction is set read-only where the engine
supports it (PostgreSQL, MySQL; SQLite opens the file read-only); rows are
capped; and the database host goes through the egress guard, so Smriti's own
database cannot be read this way. Use a read-only database account as well.

Drivers: PostgreSQL (psycopg) and SQLite are built in; MySQL/MariaDB with
`pip install pymysql`, SQL Server with `pyodbc`, Oracle with `oracledb`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..egress import EgressDenied, check_host
from .base import COMMON_FIELDS, Connector, ConnectorError, F, Item

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|copy|attach|detach|pragma|vacuum|call|exec|execute|lock|set|load_file|into\s+outfile)\b",
    re.I,
)


def check_query(sql: str) -> str:
    q = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";").strip()
    if ";" in q:
        raise ConnectorError("one statement only")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise ConnectorError("the query must start with SELECT or WITH")
    # string literals may legitimately contain these words; strip them before checking
    bare = re.sub(r"'(?:[^']|'')*'", "''", q)
    if m := FORBIDDEN.search(bare):
        raise ConnectorError(f"read-only queries only: '{m.group(1)}' is not allowed")
    return q


def _allowed_roots() -> list[Path]:
    from .files import folder_roots

    return folder_roots()


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    return str(v)


class DatabaseConnector(Connector):
    kind = "database"
    label = "Database (SQL)"
    description = "Runs one read-only query on PostgreSQL, MySQL, SQL Server, Oracle or SQLite; ingests rows as documents, or the result as a table for exact totals."
    icon = "🗄"
    fields = [
        F(
            "url",
            "Connection URL",
            "secret",
            required=True,
            help="SQLAlchemy form, e.g. postgresql+psycopg://reader:pw@db.internal:5432/works ; stored encrypted",
        ),
        F(
            "query",
            "SQL query (read-only)",
            "textarea",
            required=True,
            placeholder="SELECT id, title, body, updated_at FROM circulars WHERE status = 'published'",
        ),
        F(
            "mode",
            "Ingest as",
            "select",
            default="rows",
            options=["rows", "table"],
            group="Mapping",
            help="rows: one document per row; table: one document with all rows",
        ),
        F("id_column", "Id column", default="id", group="Mapping"),
        F("title_column", "Title column", default="title", group="Mapping"),
        F("date_column", "Date column", group="Mapping"),
        F("text_columns", "Columns to include (comma separated)", group="Mapping", help="empty = all"),
        F("table_title", "Table document title", group="Mapping", help="table mode"),
        F(
            "incremental_column",
            "Incremental column",
            group="Mapping",
            help="e.g. updated_at; later runs fetch only newer rows (rows mode)",
        ),
        F("max_rows", "Max rows", "number", default=5000, group="Mapping"),
        *COMMON_FIELDS,
    ]
    uses_network = False

    def _engine(self):  # type: ignore[no-untyped-def]
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url

        try:
            url = make_url(self.secrets["url"])
        except Exception as e:
            raise ConnectorError(f"invalid connection URL: {e}") from e
        if url.get_backend_name() == "sqlite":
            db = Path(url.database or "").resolve()
            if not any(r == db or r in db.parents for r in _allowed_roots()):
                raise ConnectorError(
                    f"SQLite files must be under SMRITI_FOLDER_ROOTS ({', '.join(map(str, _allowed_roots()))})"
                )
            if not db.exists():
                raise ConnectorError(f"no such SQLite file: {db}")
            return create_engine(f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true")
        try:
            check_host(url.host or "localhost", url.port)
        except EgressDenied as e:
            raise ConnectorError(str(e)) from e
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 10} if url.get_backend_name() == "postgresql" else {},
        )

    def discover(self) -> Iterator[Item]:
        from sqlalchemy import text

        q = check_query(self.cfg["query"])
        limit = int(self.cfg.get("max_rows") or 5000)
        inc = (self.cfg.get("incremental_column") or "").strip()
        mode = self.cfg.get("mode") or "rows"
        params: dict[str, Any] = {}
        if inc and mode == "rows":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", inc):
                raise ConnectorError("incremental column must be a plain column name")
            last = self.ctx.state.get("cursor")
            where = f" WHERE {inc} > :last" if last is not None else ""
            # q is the curator's own query, already checked read-only; inc is validated above
            q = f"SELECT * FROM ({q}) smriti_q{where} ORDER BY {inc}"  # noqa: S608
            if last is not None:
                params["last"] = last
        eng = self._engine()
        try:
            with eng.connect() as conn:
                backend = eng.dialect.name
                if backend == "postgresql":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    conn.exec_driver_sql("SET statement_timeout = 60000")
                elif backend in ("mysql", "mariadb"):
                    conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                res = conn.execute(text(q), params)
                cols = list(res.keys())
                rows = res.fetchmany(limit)
                conn.rollback()
        except ConnectorError:
            raise
        except Exception as e:
            raise ConnectorError(f"query failed: {type(e).__name__}: {str(e)[:300]}") from e
        finally:
            eng.dispose()
        if len(rows) == limit:
            self.ctx.log(f"stopped at max_rows={limit}")
        if mode == "table":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for r in rows:
                w.writerow([_cell(v) for v in r])
            data = buf.getvalue().encode("utf-8")
            title = self.cfg.get("table_title") or "Query result"
            yield Item(
                key="table",
                filename=re.sub(r"[^A-Za-z0-9-]+", "-", title.lower()).strip("-") + ".csv",
                fetch=lambda: data,
                title=title,
                hints={"title": title},
                size=len(data),
            )
            return
        idc, tc, dc = (
            self.cfg.get("id_column") or "",
            self.cfg.get("title_column") or "",
            self.cfg.get("date_column") or "",
        )
        only = [c.strip() for c in (self.cfg.get("text_columns") or "").split(",") if c.strip()]
        for r in rows:
            rec = dict(zip(cols, r, strict=False))
            rid = (
                _cell(rec.get(idc))
                if idc in rec
                else hashlib.sha256("\x1f".join(_cell(v) for v in r).encode()).hexdigest()[:16]
            )
            title = _cell(rec.get(tc)) if tc in rec and rec.get(tc) else f"Row {rid}"
            lines = [f"# {title}", ""]
            for c in only or cols:
                if c == tc or c not in rec:
                    continue
                v = _cell(rec[c])
                if not v:
                    continue
                lines += [f"## {c}", "", v, ""] if "\n" in v or len(v) > 200 else [f"{c}: {v}"]
            data = "\n".join(lines).encode("utf-8")
            hints: dict[str, Any] = {"title": title}
            if dc in rec and rec[dc] is not None:
                hints["date"] = _cell(rec[dc])[:10]
            if inc and inc in rec and rec[inc] is not None:
                cur = rec[inc]
                prev = self.ctx.state.get("cursor")
                self.ctx.state["cursor"] = _cell(cur) if prev is None or _cell(cur) > str(prev) else prev
            safe = re.sub(r"[^A-Za-z0-9_-]+", "-", rid)[:60] or "row"
            yield Item(
                key=rid, filename=f"row-{safe}.md", fetch=lambda d=data: d, title=title, hints=hints, size=len(data)
            )
