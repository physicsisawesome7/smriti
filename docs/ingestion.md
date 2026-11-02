# Ingestion: sources, uploads and jobs

Open **Ingest** in the web app as someone with the `curate` or `administer` grant. People with only the `upload` grant see the Upload and Jobs tabs.

| Kind | What it reads | Notes |
|---|---|---|
| **Website** | A whole site, breadth-first from a start page | Scope by path, domain or subdomains. Include and exclude regexes, max pages and depth, optional sitemap seeding. Linked PDF, Office and ZIP files are downloaded. Honours robots.txt and a per-host rate limit. |
| **Web page or file link** | One URL | HTML or any document. A re-sync detects a changed file and stores a new version. |
| **Upload files** | Files from the browser | Drag and drop, many at once, up to 200 MB each. ZIP archives are unpacked. Files are stored first and parsed by the worker, so the browser doesn't wait. |
| **REST / JSON API** | Records from an HTTP API | Auth: bearer, header, basic or query key. Records are found by a path such as `data.items`. Pagination: page, offset, cursor or next-URL (including the `Link` header). Title, date and link fields are mapped, the rest rendered as text. It can download a linked file per record instead. A non-JSON response, such as a PDF report endpoint, is ingested as a file. |
| **Database (SQL)** | One read-only query | PostgreSQL and SQLite are built in; MySQL needs `pymysql`, SQL Server `pyodbc`, Oracle `oracledb`. *Rows* mode makes each row a document, versioned by id, with an optional incremental column. *Table* mode makes the result one table, so totals over it are computed in code. |
| **MCP server** | A Model Context Protocol server over HTTP | *Resources* mode lists and reads every resource (text and binary), with an optional URI filter. *Tool* mode calls one tool with fixed arguments and ingests the result. Streamable HTTP or legacy SSE, optional bearer token. |
| **RSS, Atom or sitemap** | Each linked page or document | Uses each entry's title and published date. |
| **Server folder / shared drive** | Files under an allowed folder | Glob pattern, optional recursion. Only folders under `SMRITI_FOLDER_ROOTS` (Docker: the host `./imports` folder, mounted at `/imports`). |

Every source has the same **Access** settings (sensitivity, divisions, projects, need-to-know groups, document type) and **Schedule** settings (cron re-sync, max items per run, what to do when an item disappears, legal status). Items go through the same pipeline as everything else: parse, version, PII-mask, chunk, embed, supersession and audit.

## Workflow

1. **Add source**: pick a kind, or start from a demo preset (one per kind, served by Smriti itself).
2. **Test connection**: a dry run that discovers a few items and parses the first ones, showing parser, size and opening text. Nothing is ingested.
3. **Save & run**: queues a job. The worker container (or the API process on a single box) runs it.
4. **Jobs**: live progress (found, new, updated, unchanged, failed, removed, bytes), a per-item result linked to the library document, a log, and Cancel. Cancelling stops between items.
5. Re-runs are cheap. Unchanged content is recognised by hash and skipped. Changed content becomes a new version. Removed items are kept, or tombstoned if the source says so.

## Safety

| Risk | Control |
|---|---|
| A URL pointed at internal services (SSRF) | Every request and redirect hop goes through an egress guard. It blocks loopback, link-local (cloud metadata), reserved ranges and Smriti's own Postgres, Qdrant and model servers. Smriti's own API is reachable only for its `/demo-*` sample paths, so a source can't read documents as another user. Private networks are allowed by default for intranets (`SMRITI_EGRESS_ALLOW_PRIVATE=0` to block them). `SMRITI_EGRESS_ALLOWLIST` adds exceptions. |
| Credentials | Tokens, passwords and database URLs are encrypted at rest (Fernet, key from `SMRITI_SECRET_KEY` or `<data>/secret.key`). The API never returns them; the UI only shows which are set. Leaving a secret field blank when editing keeps the stored value. |
| Writing to a database | A single SELECT or WITH only. Data-changing keywords are refused, the transaction is read-only on PostgreSQL and MySQL, SQLite opens files read-only and only under allowed folders, and rows are capped. Use a read-only database account as well. |
| Running local programs via MCP | Only network transports can be configured. Stdio servers would execute commands on the host. |
| One source loosening another's access | The same content arriving from a second source with stricter access **tightens** the document so both rules hold. It never loosens. |
| Who can do what | `curate` or `administer` manage sources and see all jobs. `upload` can upload and see their own jobs. Every create, change, run, preview and cancel is in the hash-chained audit log. |

## Scaling

Jobs are rows in the database. Any number of worker processes can claim them atomically. A job whose worker dies is marked failed after 15 minutes, so it can be re-run. Scheduled sources are queued by the scheduler and executed by the workers, so the API stays responsive while a large crawl runs.
