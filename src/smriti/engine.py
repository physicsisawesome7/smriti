"""Tenant runtime: ingestion and permission-aware, version-aware retrieval.

One ``Tenant`` per tenant pack. It owns the index for that tenant and talks to
the shared relational store and blob store. API replicas are stateless apart
from this in-process index, which is rebuilt from the store on start (vectors
come from the embedding cache, so a restart costs no model calls); with the
Qdrant backend there is nothing to rebuild.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import struct
import threading
import time
import zipfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from .access import AccessFilter, Directory, LocalAuthorizer
from .audit import AuditLog
from .chunking import render_rows, render_whole
from .config import Settings, TenantPack
from .guardrails import Guardrails
from .index import (
    Embedder,
    HashingEmbedder,
    HttpReranker,
    LexicalReranker,
    LocalIndex,
    OpenAICompatEmbedder,
    QdrantIndex,
    Reranker,
    VectorIndex,
    tokenize,
)
from .llm import Gateway
from .metadata import extract, front_matter
from .models import Access, Passage, Principal
from .parsers import parse, sniff
from .privacy import mask_table, mask_text
from .store import BlobStore, Store, memories, relations, text_hash

MAX_FILE_MB = 100
ZIP_DEPTH = 2
ZIP_MAX_ENTRIES = 2000
ZIP_MAX_TOTAL_MB = 1024
ZIP_MAX_RATIO = 100


class Tenant:
    def __init__(self, pack: TenantPack, store: Store, blobs: BlobStore, settings: Settings):
        self.pack = pack
        self.id = pack.id
        self.store = store
        self.blobs = blobs
        self.settings = settings
        self.audit = AuditLog(store, self.id)
        self.directory = Directory(pack.roles)
        self.authz = LocalAuthorizer()
        self.gw = Gateway(pack.config.models)
        self.guard = Guardrails(pack.guardrails, self.gw)
        self.embedder: Embedder = (
            OpenAICompatEmbedder(pack.config.models.embed)
            if pack.config.models.embed and pack.config.models.embed.base_url
            else HashingEmbedder()
        )
        self.reranker: Reranker = (
            HttpReranker(pack.config.models.rerank)
            if pack.config.models.rerank and pack.config.models.rerank.base_url
            else LexicalReranker(self._idf)
        )
        self.index: VectorIndex = (
            QdrantIndex(
                settings.qdrant_url or "http://localhost:6333",
                # one collection per embedding model: a model upgrade re-indexes side by side (blue-green)
                f"smriti_{self.id}_{re.sub(r'[^a-z0-9]+', '_', self.embedder.name.lower()).strip('_')}",
                self.embedder.dim,
            )
            if settings.vector_backend == "qdrant"
            else LocalIndex(self.embedder.dim)
        )
        self._gen: tuple[float, int] = (0.0, -1)
        self._visible: dict[str, tuple[int, frozenset[str]]] = {}
        self._qvecs: dict[str, np.ndarray] = {}
        self._ingest_lock = threading.Lock()
        self._rebuild()

    # ------------------------------------------------------------------ helpers

    def _idf(self, term: str) -> float:
        if isinstance(self.index, LocalIndex):
            n = max(1, len(self.index.ids))
            df = len(self.index.postings.get(term, {}))
            return float(np.log(1 + (n - df + 0.5) / (df + 0.5)))
        return 1.0

    def _index_text(self, c: dict[str, Any]) -> str:
        body = c["text"] if self.pack.config.retrieval.index_pii else c["text_masked"]
        return f"{c['header']}\n{body}"

    def _payload(self, c: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "chunk_id": c["chunk_id"],
            "doc_id": c["doc_id"],
            "doc_type": doc["doc_type"],
            "access": doc["access"],
            "current": c.get("last_version") is None and doc["status"] == "current",
            "clause_id": c.get("clause_id"),
            "lang": c.get("lang", "en"),
        }

    def _embed(self, texts: list[str]) -> np.ndarray:
        hashes = [text_hash(t) for t in texts]
        cached = self.store.get_embeddings(self.embedder.name, hashes)
        missing = [i for i, h in enumerate(hashes) if h not in cached]
        out = np.zeros((len(texts), self.embedder.dim), dtype=np.float32)
        if missing:
            vecs = self.embedder.embed([texts[i] for i in missing])
            self.store.put_embeddings(
                self.embedder.name,
                {hashes[i]: vecs[j].astype(np.float32).tobytes() for j, i in enumerate(missing)},
                self.embedder.dim,
            )
            for j, i in enumerate(missing):
                out[i] = vecs[j]
        for i, h in enumerate(hashes):
            if h in cached:
                out[i] = np.frombuffer(cached[h], dtype=np.float32)
        return out

    def _rebuild(self) -> None:
        """Fill the index from the store: always for the in-process index, and for
        Qdrant when this embedding model's collection is empty (a model change
        builds the new collection side by side; the old one stays until removed)."""
        if not isinstance(self.index, LocalIndex) and len(self.index) > 0:
            return
        rows = self.store.all_chunks(self.id)
        if not rows:
            return
        docs = {d["doc_id"]: d for d in self.store.list_documents(self.id)}
        rows = [r for r in rows if r["doc_id"] in docs and docs[r["doc_id"]]["status"] != "tombstoned"]
        texts = [self._index_text(r) for r in rows]
        if not isinstance(self.index, LocalIndex):
            print(f"{self.id}: indexing {len(rows)} chunks with {self.embedder.name}", flush=True)
        for i in range(0, len(rows), 256):
            part, ptexts = rows[i : i + 256], texts[i : i + 256]
            vecs = self._embed(ptexts)
            self.index.upsert(
                [(r["chunk_id"], vecs[j], ptexts[j], self._payload(r, docs[r["doc_id"]])) for j, r in enumerate(part)]
            )

    def principal(self, user_id: str) -> Principal:
        return self.directory.principal(user_id)

    @property
    def generation(self) -> int:
        """Index generation, shared through the database so every API worker and
        the ingest worker agree on when cached answers went stale. Read at most
        once a second."""
        now = time.monotonic()
        if now - self._gen[0] > 1.0:
            self._gen = (now, self.store.get_counter(self.id, "generation"))
        return self._gen[1]

    def bump_generation(self) -> None:
        self.store.incr_counter(self.id, "generation")
        self._gen = (0.0, -1)

    def visible_docs(self, p: Principal) -> frozenset[str]:
        """Documents this person may read, computed once per access profile and
        index generation, so search filtering is a set lookup, not a rule
        evaluation per chunk. The bulk check after search still re-evaluates."""
        gen = self.generation
        hit = self._visible.get(p.access_key)
        if hit and hit[0] == gen:
            return hit[1]
        docs = self.store.list_documents(self.id)
        allowed = frozenset(self.authz.bulk_check(p, {d["doc_id"]: d for d in docs}))
        if len(self._visible) > 512:
            self._visible.clear()
        self._visible[p.access_key] = (gen, allowed)
        return allowed

    def query_vector(self, query: str) -> np.ndarray:
        expanded, _, _ = self.expand_query(query)
        v = self._qvecs.get(expanded)
        if v is None:
            v = self._embed_query(expanded)
            if len(self._qvecs) > 4096:
                self._qvecs.clear()
            self._qvecs[expanded] = v
        return v

    # ------------------------------------------------------------------ ingestion

    def ingest(
        self,
        data: bytes,
        filename: str,
        *,
        actor: str,
        source: str | None = None,
        source_url: str | None = None,
        hints: dict[str, Any] | None = None,
        doc_id: str | None = None,
        legal_status: str | None = None,
        trust: str = "content",
        apply_relations: bool | None = None,
        _depth: int = 0,
    ) -> dict[str, Any]:
        """Parse, version, mask, index and link one file.

        ``trust`` says how far the file itself may decide things about the library:

        * ``pack``: tenant packs, seeding and memories. Front matter may set the
          document id, access and supersession.
        * ``curator``: an upload or connector run by someone with ``curate``.
          The caller's access hints apply as given, an existing document may get a
          new version, and supersession found in the text is applied.
        * ``content`` (default): everything else. Front matter cannot set the id,
          access or relations; a file can only become a new version of a document
          its own uploader or source created; access on a new version only ever
          tightens; supersession found in the text is proposed for a curator to
          approve instead of retiring anything.
        """
        if trust not in TRUST_LEVELS:
            raise ValueError(f"unknown trust level {trust!r}")
        if apply_relations is None:
            apply_relations = trust != "content"
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise ValueError(f"{filename}: larger than {MAX_FILE_MB} MB")
        mime = sniff(data, filename)
        if mime == "application/zip":
            return self._ingest_zip(
                data,
                filename,
                actor=actor,
                source=source,
                hints=hints,
                depth=_depth,
                trust=trust,
                apply_relations=apply_relations,
            )
        scan = scan_malware(data)
        if scan.get("error"):
            # a configured scanner that cannot vouch for the file is a failure, not a pass
            self.audit.record(actor, "malware_scan_failed", filename=filename, error=str(scan["error"])[:300])
            if os.environ.get("SMRITI_MALWARE_FAIL_OPEN") != "1":
                raise ValueError(f"{filename}: malware scan failed ({scan['error']}); not ingested")
        if scan.get("infected"):
            self.audit.record(
                actor, "ingest_rejected", filename=filename, reason="malware", signature=scan.get("signature")
            )
            raise ValueError(f"{filename}: rejected by malware scan ({scan.get('signature')})")

        with self._ingest_lock:
            sha = hashlib.sha256(data).hexdigest()
            # only the version currently in force of a live document counts as a duplicate:
            # reverting a policy to older text, or re-uploading a deleted file, is new content
            dup = self.store.current_version_by_sha(self.id, sha)
            if dup and ((self.store.get_document(self.id, dup["doc_id"]) or {}).get("meta") or {}).get("index_pending"):
                self.reindex_document(dup["doc_id"], actor)
                return {"status": "reindexed", "doc_id": dup["doc_id"], "version": dup["version"]}
            if dup:
                tightened = self._tighten_access(dup["doc_id"], (hints or {}).get("access"), actor)
                return {
                    "status": "duplicate",
                    "doc_id": dup["doc_id"],
                    "version": dup["version"],
                    "access_tightened": tightened,
                }

            fm = front_matter(data)
            ignored = sorted(k for k in fm if k in PROTECTED_FRONT_MATTER) if trust != "pack" else []
            if ignored:  # a file must not decide its own id, access or what it retires
                fm = {k: v for k, v in fm.items() if k not in PROTECTED_FRONT_MATTER}
            merged: dict[str, Any] = {**(hints or {}), **fm}
            parsed = parse(data, filename)
            if parsed.parser == "email" and "correspondence" in self.pack.schemas:
                merged.setdefault("doc_type", "correspondence")
            title = str(merged.get("title") or parsed.title)
            meta = extract(parsed.text, title, self.pack.schemas, merged)
            # Only a declared reference names the document; a reference merely found in the
            # text (a letter quoting a tender number) must not turn it into a version of that tender.
            ref_declared = bool(merged.get("ref_no"))
            named = bool(doc_id or merged.get("doc_id"))
            doc_id = (
                doc_id
                or merged.get("doc_id")
                or _derive_doc_id(source, source_url, meta.get("ref_no") if ref_declared else None, filename)
            )
            existing = self.store.get_document(self.id, doc_id)
            if existing and trust == "content" and not _may_version(existing, actor, source, named):
                # a same-named file from someone else becomes its own document; the response
                # is the same whether or not the colliding document is visible to the uploader
                doc_id = f"{doc_id}-{sha[:10]}"[:128]
                existing = self.store.get_document(self.id, doc_id)
            version = (existing["current_version"] + 1) if existing else 1

            requested = merged.get("access")
            loosened = False
            if existing and requested and trust != "pack":
                from .access import strictest

                old = Access.from_dict(existing["access"])
                new = Access.from_dict(requested)
                if trust == "curator":
                    access = new
                    loosened = not _no_stricter(new, old)  # the curator removed a restriction
                else:
                    access = strictest([old.to_dict(), new.to_dict()])
            else:
                access = Access.from_dict(requested or (existing["access"] if existing else None))
            if not doc_id_ok(doc_id):
                raise ValueError(f"{filename}: invalid document id")
            schema = self.pack.schemas.get(meta["doc_type"], {})
            name_cols = schema.get("pii_columns") or None

            def mask_rows(header: list[str], rows: list[list[str]]) -> list[list[str]]:
                kw = {"name_columns": name_cols} if name_cols else {}
                return mask_table(header, rows, (), **kw)[0]

            from .chunking import chunk_document

            chunks, tables = chunk_document(doc_id, parsed, meta, mask_rows=mask_rows)
            if not chunks:
                parsed.warnings.append(
                    "no text found: a scanned image, an empty file or a page rendered by JavaScript; nothing is searchable"
                )
            pii: set[str] = set(access.pii)
            for c in chunks:
                if c.kind in ("table", "table_rows"):
                    if c.text_masked != c.text:
                        pii.add("land_owner_name")
                    mt, cats = mask_text(c.text_masked)
                    c.text_masked = mt
                else:
                    c.text_masked, cats = mask_text(c.text)
                pii |= cats
            access.pii = sorted(pii)

            raw_key = self.blobs.put(data, Path(filename).suffix[:10])
            parse_key = self.blobs.put(
                json.dumps([asdict(b) for b in parsed.blocks], ensure_ascii=False).encode("utf-8"), ".json"
            )

            injection_blocks = sum(1 for c in chunks if self.guard.scan_passage(c.text))
            doc = {
                "doc_id": doc_id,
                "title": title,
                "doc_type": meta["doc_type"],
                "ref_no": meta.get("ref_no"),
                "doc_date": meta.get("doc_date"),
                "source": source or (existing or {}).get("source") or "upload",
                "source_url": source_url or (existing or {}).get("source_url"),
                "access": access.to_dict(),
                "status": "current" if not existing or existing["status"] != "superseded" else "superseded",
                "current_version": version,
                "meta": {
                    **((existing or {}).get("meta") or {}),
                    "aliases": list(merged.get("aliases") or ((existing or {}).get("meta") or {}).get("aliases") or []),
                    "effective_from": meta.get("effective_from"),
                    "ref_declared": ref_declared,
                    "lang": _majority([c.lang for c in chunks]) if chunks else "en",
                    "pages": parsed.pages,
                    "parser": parsed.parser,
                    "low_confidence": parsed.low_confidence,
                    "warnings": parsed.warnings,
                    "injection_blocks": injection_blocks,
                    "legal_status": legal_status or merged.get("legal_status") or "customer-owned",
                    "sha256": sha,
                    "owner": ((existing or {}).get("meta") or {}).get("owner") or actor,
                    "trust": trust,
                    "ignored_front_matter": ignored,
                },
            }
            # Embed first: if the embedder is down, nothing has been written and a retry is clean.
            old_ids = self.store.current_chunk_ids(self.id, doc_id)
            new_by_id = {c.chunk_id: c for c in chunks}
            added = [c for cid, c in new_by_id.items() if cid not in old_ids]
            removed = old_ids - set(new_by_id)
            rows = [
                {
                    "chunk_id": c.chunk_id,
                    "doc_id": doc_id,
                    "first_version": version,
                    "last_version": None,
                    "text": c.text,
                    "text_masked": c.text_masked,
                    "header": c.header,
                    "section_path": json.dumps(c.section_path, ensure_ascii=False),
                    "clause_id": c.clause_id,
                    "page": c.page,
                    "kind": c.kind,
                    "lang": c.lang,
                    "confidence": c.confidence,
                    "table_id": c.table_id,
                    "text_hash": text_hash(c.text),
                }
                for c in added
            ]
            dict_rows = [r | {"section_path": json.loads(r["section_path"])} for r in rows]
            texts = [self._index_text(r) for r in dict_rows]
            vecs = self._embed(texts) if texts else None

            self.store.upsert_document(self.id, doc)
            self.store.add_version(
                self.id,
                {
                    "doc_id": doc_id,
                    "version": version,
                    "sha256": sha,
                    "blob_key": raw_key,
                    "parse_key": parse_key,
                    "filename": filename,
                    "mime": parsed.mime,
                    "parser": parsed.parser,
                    "effective_from": meta.get("effective_from"),
                    "valid_until": meta.get("valid_until"),
                    "pages": parsed.pages,
                    "low_confidence": parsed.low_confidence,
                    "warnings": json.dumps(parsed.warnings),
                },
            )

            self.store.close_chunks(self.id, removed, version - 1)
            self.store.insert_chunks(self.id, rows)
            for t in tables:
                self.store.put_table(self.id, {**t, "version": version})

            # The database now holds the version. If the vector store fails, the document is
            # marked index_pending: a retry or the reconcile job finishes indexing it.
            try:
                if texts:
                    self.index.upsert(
                        [(r["chunk_id"], vecs[i], texts[i], self._payload(r, doc)) for i, r in enumerate(dict_rows)]
                    )
                if removed:
                    self.index.set_payload(list(removed), {"current": False})
                kept = [cid for cid in new_by_id if cid in old_ids]
                if kept:
                    self.index.set_payload(
                        kept,
                        {"access": doc["access"], "doc_type": doc["doc_type"], "current": doc["status"] == "current"},
                    )
            except Exception as e:
                self._mark_index_pending(doc_id, True)
                self.audit.record(actor, "index_failed", doc_id=doc_id, version=version, error=str(e)[:300])
                raise RuntimeError(f"{filename}: stored, but indexing failed ({e}); it will be retried") from e

            impact = self._apply_relations(doc_id, meta, actor, "pack" if apply_relations else "content")
            self.bump_generation()
        if loosened:
            self.audit.record(actor, "access_changed", doc_id=doc_id, access=access.to_dict())
        if ignored:
            self.audit.record(actor, "front_matter_ignored", doc_id=doc_id, keys=ignored, trust=trust)

        self.audit.record(
            actor,
            "ingest",
            doc_id=doc_id,
            version=version,
            sha256=sha,
            source=doc["source"],
            chunks_added=len(added),
            chunks_retired=len(removed),
            chunks_unchanged=len(new_by_id) - len(added),
            doc_class=access.doc_class,
            pii=access.pii,
        )
        return {
            "status": "ingested" if version == 1 else "new_version",
            "doc_id": doc_id,
            "version": version,
            "title": title,
            "doc_type": meta["doc_type"],
            "ref_no": meta.get("ref_no"),
            "chunks_added": len(added),
            "chunks_retired": len(removed),
            "chunks_unchanged": len(new_by_id) - len(added),
            "warnings": parsed.warnings,
            "relations": meta["relations"],
            "impact": impact,
        }

    def _mark_index_pending(self, doc_id: str, pending: bool) -> None:
        d = self.store.get_document(self.id, doc_id)
        if not d:
            return
        meta = dict(d.get("meta") or {})
        if bool(meta.get("index_pending")) == pending:
            return
        meta["index_pending"] = pending
        if not pending:
            meta.pop("index_pending", None)
        d["meta"] = meta
        self.store.upsert_document(self.id, d)

    def reindex_document(self, doc_id: str, actor: str = "system") -> int:
        """Embed and index a document's current chunks again (after an indexing failure)."""
        d = self.store.get_document(self.id, doc_id)
        if not d:
            raise KeyError(doc_id)
        rows = self.store.doc_chunks(self.id, doc_id)
        texts = [self._index_text(r) for r in rows]
        if texts:
            vecs = self._embed(texts)
            self.index.upsert([(r["chunk_id"], vecs[i], texts[i], self._payload(r, d)) for i, r in enumerate(rows)])
        self._mark_index_pending(doc_id, False)
        self.bump_generation()
        self.audit.record(actor, "reindexed", doc_id=doc_id, chunks=len(rows))
        return len(rows)

    def reconcile_index(self, actor: str = "system") -> list[str]:
        """Finish indexing every document whose indexing failed. Safe to run any time."""
        done = []
        for d in self.store.list_documents(self.id):
            if (d.get("meta") or {}).get("index_pending") and d["status"] != "tombstoned":
                try:
                    self.reindex_document(d["doc_id"], actor)
                    done.append(d["doc_id"])
                except Exception:  # noqa: S112 - the vector store is still down; try again next time
                    continue
        return done

    def _ingest_zip(
        self,
        data: bytes,
        filename: str,
        *,
        actor: str,
        source: str | None,
        hints: dict[str, Any] | None,
        depth: int,
        trust: str = "content",
        apply_relations: bool | None = None,
    ) -> dict[str, Any]:
        if depth >= ZIP_DEPTH:
            raise ValueError(f"{filename}: archive nesting deeper than {ZIP_DEPTH}")
        results = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            total = sum(i.file_size for i in infos)
            packed = max(1, sum(i.compress_size for i in infos))
            if len(infos) > ZIP_MAX_ENTRIES:
                raise ValueError(f"{filename}: more than {ZIP_MAX_ENTRIES} files in one archive")
            if total > ZIP_MAX_TOTAL_MB * 1024 * 1024 or total / packed > ZIP_MAX_RATIO:
                raise ValueError(f"{filename}: archive expands too far (possible zip bomb); not ingested")
            for info in z.infolist():
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    raise ValueError(f"{filename}: encrypted archive; the owner must supply the key")
                if info.file_size > MAX_FILE_MB * 1024 * 1024:
                    continue
                inner = z.read(info)
                results.append(
                    self.ingest(
                        inner,
                        info.filename,
                        actor=actor,
                        source=source,
                        hints=hints,
                        trust=trust,
                        apply_relations=apply_relations,
                        _depth=depth + 1,
                    )
                )
        return {"status": "archive", "filename": filename, "members": results}

    def _apply_relations(
        self, doc_id: str, meta: dict[str, Any], actor: str, trust: str = "pack"
    ) -> list[dict[str, Any]]:
        """Write supersession edges, retire superseded documents, and list what a
        modifying document touches (the circular impact analysis).

        Relations from ``content``-trust files are stored as proposed: they take
        effect only once a curator approves them, so an upload or a crawled page
        cannot retire a real circular."""
        effective = meta.get("effective_from") or meta.get("doc_date")
        status = "proposed" if trust == "content" else "auto"
        for rel in meta.get("relations", []):
            dst = self.store.find_by_ref(self.id, rel["dst_ref"])
            if dst and dst["doc_id"] == doc_id:
                continue
            if status == "proposed" and rel["rel_type"] != "refers_to":
                self.audit.record(
                    actor, "relation_proposed", src_doc=doc_id, rel_type=rel["rel_type"], dst_ref=rel["dst_ref"]
                )
            self.store.add_relation(
                self.id,
                {
                    "src_doc": doc_id,
                    "rel_type": rel["rel_type"],
                    "dst_doc": dst["doc_id"] if dst else None,
                    "dst_ref": rel["dst_ref"],
                    "clause_id": rel.get("clause_id"),
                    "effective_from": effective,
                    "evidence": rel.get("evidence"),
                    "status": "auto" if rel["rel_type"] == "refers_to" else status,
                },
            )
        self.store.resolve_relations(self.id)
        self.retire_superseded(actor)
        return self.impact_of(doc_id)

    def review_relation(self, rid: int, approve: bool, actor: str) -> dict[str, Any]:
        """A curator approves or rejects a proposed supersession or amendment."""
        rows = self.store.rows(relations, tenant=self.id, id=rid)
        if not rows:
            raise KeyError(f"no relation {rid}")
        self.store.update_row(relations, {"id": rid, "tenant": self.id}, status="approved" if approve else "rejected")
        self.audit.record(actor, "relation_approved" if approve else "relation_rejected", relation_id=rid)
        if approve:
            self.retire_superseded(actor)
        self.bump_generation()
        return {**rows[0], "status": "approved" if approve else "rejected"}

    def retire_superseded(self, actor: str) -> None:
        # supersession: a document superseded by one already in force stops being current
        today = date.today().isoformat()
        for r in self.store.all_relations(self.id):
            if r["rel_type"] == "supersedes" and r["dst_doc"] and (r["effective_from"] or "") <= today:
                d = self.store.get_document(self.id, r["dst_doc"])
                if d and d["status"] == "current":
                    self.store.set_doc_status(self.id, r["dst_doc"], "superseded")
                    self.index.set_payload_where(r["dst_doc"], {"current": False})
                    self._flag_memories(r["dst_doc"])
                    self.audit.record(actor, "superseded", doc_id=r["dst_doc"], by=r["src_doc"])

    def _flag_memories(self, doc_id: str) -> None:
        for m in self.store.rows(memories, tenant=self.id):
            if doc_id in json.loads(m["provenance"] or "[]"):
                self.store.update_row(memories, {"id": m["id"]}, needs_revalidation=True)

    def impact_of(self, doc_id: str) -> list[dict[str, Any]]:
        rels = self.store.all_relations(self.id)
        out = []
        for r in rels:
            if r["src_doc"] != doc_id or r["rel_type"] not in ("modifies", "supersedes") or not r["dst_doc"]:
                continue
            for ref in rels:
                if ref["rel_type"] != "refers_to" or ref["dst_doc"] != r["dst_doc"] or ref["src_doc"] == doc_id:
                    continue
                if r["clause_id"] and ref["clause_id"] and not _clause_match(r["clause_id"], ref["clause_id"]):
                    continue
                d = self.store.get_document(self.id, ref["src_doc"])
                if d and d["status"] != "tombstoned":
                    out.append(
                        {
                            "doc_id": d["doc_id"],
                            "title": d["title"],
                            "doc_type": d["doc_type"],
                            "clause_id": ref["clause_id"],
                            "via": r["dst_doc"],
                            "change": r["rel_type"],
                            "access": d["access"],
                            "evidence": ref["evidence"],
                        }
                    )
        seen = set()
        uniq = []
        for o in out:
            k = (o["doc_id"], o["clause_id"])
            if k not in seen:
                seen.add(k)
                uniq.append(o)
        return uniq

    def _tighten_access(self, doc_id: str, requested: dict[str, Any] | None, actor: str) -> bool:
        """The same content arriving from a second source with stricter access rules
        makes the document stricter (both sources' rules must hold). It never
        loosens access: first-come must not decide who can read a file."""
        if not requested:
            return False
        d = self.store.get_document(self.id, doc_id)
        if not d:
            return False
        from .access import strictest

        current = Access.from_dict(d["access"])
        merged = strictest([d["access"], {**Access.from_dict(requested).to_dict(), "pii": current.pii}])
        merged.pii = current.pii
        if merged.to_dict() == current.to_dict() or _no_stricter(current, Access.from_dict(requested)):
            return False
        d["access"] = merged.to_dict()
        self.store.upsert_document(self.id, d)
        self.index.set_payload_where(doc_id, {"access": d["access"]})
        self.bump_generation()
        self.audit.record(actor, "access_tightened", doc_id=doc_id, access=d["access"])
        return True

    def tombstone(self, doc_id: str, actor: str, reason: str) -> None:
        self.store.set_doc_status(self.id, doc_id, "tombstoned")
        self.index.set_payload_where(
            doc_id,
            {
                "current": False,
                "access": {
                    "doc_class": "secret",
                    "divisions": [],
                    "projects": ["__tombstoned__"],
                    "groups": ["__tombstoned__"],
                    "pii": [],
                    "extra_rules": [],
                },
            },
        )
        self.bump_generation()
        self.audit.record(actor, "tombstone", doc_id=doc_id, reason=reason)

    # ------------------------------------------------------------------ retrieval

    def expand_query(self, query: str) -> tuple[str, list[str], str]:
        """Glossary expansion (LOA -> letter of acceptance) and cross-language synonyms.

        Returns (text to search with, expansions, text to judge relevance with).
        Search uses every expansion; relevance uses the question plus
        abbreviation expansions, and synonyms only when they cross scripts
        (a Hindi question judged against English passages), so a Hindi synonym
        never dilutes an English question.
        """
        g = self.pack.glossary
        extra: list[str] = []
        focus: list[str] = []
        low = query.lower()
        hindi_q = bool(re.search(r"[ऀ-ॿ]", query))
        for abbr, full in (g.get("abbreviations") or {}).items():
            if re.search(rf"(?<![\w]){re.escape(abbr.lower())}(?![\w])", low):
                extra.append(str(full))
                focus.append(str(full))
        for term, syns in (g.get("synonyms") or {}).items():
            names = [term, *([syns] if isinstance(syns, str) else syns)]
            if any(re.search(rf"(?<![\wऀ-ॿ]){re.escape(n.lower())}", low) for n in names):
                new = [str(n) for n in names if n.lower() not in low]
                extra.extend(new)
                if hindi_q:  # a Hindi question is judged against English passages too
                    focus.extend(n for n in new if not re.search(r"[ऀ-ॿ]", n))
        searched = query + (" " + " ".join(extra) if extra else "")
        return searched, extra, query + (" " + " ".join(focus) if focus else "")

    def retrieve(
        self,
        p: Principal,
        query: str,
        *,
        as_of: str | None = None,
        include_superseded: bool = False,
        top_k: int | None = None,
        doc_ids: list[str] | None = None,
        qvec: np.ndarray | None = None,
    ) -> tuple[list[Passage], dict[str, Any]]:
        t0 = time.perf_counter()
        rc = self.pack.config.retrieval
        top_k = top_k or rc.top_k
        expanded, extra, focus_text = self.expand_query(query)
        qtokens = tokenize(expanded)
        focus = tokenize(focus_text)
        if qvec is None:
            qvec = self.query_vector(query)
        flt = AccessFilter(p)
        # first enforcement point: the set of readable documents, applied inside the search
        allow: frozenset[str] = self.visible_docs(p)
        if doc_ids:
            allow = allow & frozenset(doc_ids)
        current_only = as_of is None and not include_superseded
        t1 = time.perf_counter()
        hits = self.index.search(
            qvec,
            qtokens,
            allow,
            flt.to_qdrant() if isinstance(self.index, QdrantIndex) else None,
            rc.candidates,
            rc.rrf_k,
            current_only,
        )
        t2 = time.perf_counter()

        docs = self.store.documents_by_ids(self.id, [h.payload["doc_id"] for h in hits])
        allowed = self.authz.bulk_check(p, docs)  # second enforcement point
        dropped = sum(1 for h in hits if h.payload["doc_id"] not in allowed)
        hits = [h for h in hits if h.payload["doc_id"] in allowed]
        chunk_rows = self.store.get_chunks(self.id, [h.chunk_id for h in hits])

        kept: list[tuple[Any, dict[str, Any], str]] = []
        versions_cache: dict[str, list[dict[str, Any]]] = {}
        sup = self._superseded_by()
        overridden = self._overridden_amendments(as_of or date.today().isoformat())
        for h in hits:
            c = chunk_rows.get(h.chunk_id)
            if not c:
                continue
            d = docs[c["doc_id"]]
            status = "current"
            if as_of:
                vs = versions_cache.setdefault(d["doc_id"], self.store.get_versions(self.id, d["doc_id"]))
                in_force = [
                    v
                    for v in vs
                    if (v["effective_from"] or "0000") <= as_of and (not v["valid_until"] or v["valid_until"] > as_of)
                ]
                if not in_force:
                    continue
                v_star = max(v["version"] for v in in_force)
                if not (c["first_version"] <= v_star and (c["last_version"] is None or c["last_version"] >= v_star)):
                    continue
                by = sup.get(d["doc_id"])
                if by and (by[1] or "") <= as_of and not include_superseded:
                    continue
                status = "historic" if (v_star != d["current_version"] or d["status"] == "superseded") else "current"
            else:
                if c["last_version"] is not None:
                    if not include_superseded:
                        continue
                    status = "historic"
                if d["status"] == "superseded":
                    if not include_superseded:
                        continue
                    status = "superseded"
            if d["doc_id"] in overridden and status == "current":
                # an earlier slip whose every amendment has been replaced by a later one
                if not include_superseded:
                    continue
                status = "superseded"
            kept.append((h, c, status))

        # rerank the authorised, in-force candidates
        texts = [f"{c['header']}\n{c['text_masked']}" for _, c, _ in kept]
        scores = self.reranker.score(focus_text, focus, texts, [h.score for h, _, _ in kept]) if kept else []
        order = sorted(range(len(kept)), key=lambda i: scores[i], reverse=True)
        t3 = time.perf_counter()

        passages: list[Passage] = []
        used: set[str] = set()
        for i in order:
            if len(passages) >= top_k:
                break
            h, c, status = kept[i]
            if c["chunk_id"] in used:
                continue
            used.add(c["chunk_id"])
            passages.append(self._passage(p, len(passages) + 1, c, docs[c["doc_id"]], scores[i], status))
            # version rule: a clause comes with the latest amendment in force on the date asked
            if c.get("clause_id"):
                for amend in self._amendments(p, c, as_of or date.today().isoformat())[-1:]:
                    if amend[0]["chunk_id"] not in used and len(passages) < top_k + 4:
                        used.add(amend[0]["chunk_id"])
                        passages.append(
                            self._passage(
                                p, len(passages) + 1, amend[0], amend[1], scores[i], "amendment", note=amend[2]
                            )
                        )
        t4 = time.perf_counter()
        trace = {
            "query_expanded": expanded if extra else None,
            "glossary": extra,
            "candidates": len(hits) + dropped,
            "dropped_by_bulk_check": dropped,
            "after_version_rules": len(kept),
            "current_only": current_only,
            "as_of": as_of,
            "reranker": self.reranker.name,
            "embedder": self.embedder.name,
            "top_scores": [round(scores[i], 3) for i in order[:top_k]],
            "timings_ms": {
                "prepare": round((t1 - t0) * 1000, 1),
                "search": round((t2 - t1) * 1000, 1),
                "authorise_and_version": round((t3 - t2) * 1000, 1),
                "assemble": round((t4 - t3) * 1000, 1),
            },
            "qtokens": focus,
        }
        return passages, trace

    def _embed_query(self, q: str) -> np.ndarray:
        if isinstance(self.embedder, OpenAICompatEmbedder):
            return self.embedder.embed([q], query=True)[0]
        return self.embedder.embed([q])[0]

    def _overridden_amendments(self, when: str) -> set[str]:
        """Slips all of whose clause amendments were replaced by later slips in force on ``when``."""
        latest: dict[tuple[str, str], tuple[str, str]] = {}
        mods: dict[str, list[tuple[str, str]]] = {}
        for r in self.store.all_relations(self.id):
            if (
                r["rel_type"] != "modifies"
                or not r["dst_doc"]
                or not r["clause_id"]
                or (r["effective_from"] or "") > when
            ):
                continue
            key = (r["dst_doc"], r["clause_id"])
            mods.setdefault(r["src_doc"], []).append(key)
            if key not in latest or (r["effective_from"] or "") > latest[key][0]:
                latest[key] = (r["effective_from"] or "", r["src_doc"])
        return {src for src, keys in mods.items() if all(latest[k][1] != src for k in keys)}

    def _superseded_by(self) -> dict[str, tuple[str, str | None]]:
        return {
            r["dst_doc"]: (r["src_doc"], r["effective_from"])
            for r in self.store.all_relations(self.id)
            if r["rel_type"] == "supersedes" and r["dst_doc"]
        }

    def _amendments(
        self, p: Principal, c: dict[str, Any], when: str
    ) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
        out = []
        rels = [
            r
            for r in self.store.relations_for(self.id, c["doc_id"])
            if r["rel_type"] == "modifies"
            and r["dst_doc"] == c["doc_id"]
            and r["clause_id"]
            and _clause_match(r["clause_id"], c["clause_id"])
            and (r["effective_from"] or "") <= when
        ]
        if not rels:
            return out
        docs = self.store.documents_by_ids(self.id, [r["src_doc"] for r in rels])
        ok = self.authz.bulk_check(p, docs)
        for r in sorted(rels, key=lambda r: r["effective_from"] or ""):
            if r["src_doc"] not in ok:
                continue
            cands = self.store.doc_chunks(self.id, r["src_doc"])
            src_chunks = [x for x in cands if x.get("clause_id") and _clause_match(r["clause_id"], x["clause_id"])]
            src_chunks = src_chunks or [x for x in cands if mentions_clause(x["text"], r["clause_id"])]
            if src_chunks:
                d = docs[r["src_doc"]]
                out.append((src_chunks[0], d, f"Amends clause {r['clause_id']} w.e.f. {r['effective_from']}"))
        return out

    def _passage(
        self,
        p: Principal,
        n: int,
        c: dict[str, Any],
        d: dict[str, Any],
        score: float,
        status: str,
        note: str | None = None,
    ) -> Passage:
        text, masked = self.render_for(p, c, d)
        return Passage(
            n=n,
            chunk_id=c["chunk_id"],
            doc_id=d["doc_id"],
            version=c["first_version"] if c["last_version"] is None else c["last_version"],
            title=d["title"],
            ref_no=d.get("ref_no"),
            doc_date=d.get("doc_date"),
            doc_type=d["doc_type"],
            clause_id=c.get("clause_id"),
            section=" > ".join(c["section_path"][-3:]),
            page=c.get("page") or 1,
            text=text,
            score=round(float(score), 3),
            status=status,
            doc_class=d["access"]["doc_class"],
            confidence=c.get("confidence", 1.0),
            masked=masked,
            note=note or ("low-confidence parse" if (c.get("confidence") or 1) < 0.6 else None),
            injection_flag=self.guard.scan_passage(c["text"]),
        )

    def render_for(self, p: Principal, c: dict[str, Any], d: dict[str, Any]) -> tuple[str, bool]:
        """Field-level masking by role: the document is visible, its PII may not be."""
        pii = set(d["access"].get("pii") or [])
        if not pii:
            return c["text"], False
        if not p.unmask:
            return c["text_masked"], c["text_masked"] != c["text"]
        if pii <= {u.lower() for u in p.unmask}:
            return c["text"], False
        if c.get("table_id"):
            t = self.store.get_table(self.id, c["table_id"])
            if t:
                header = t["columns"]
                schema = self.pack.schemas.get(d["doc_type"], {})
                kw = {"name_columns": schema["pii_columns"]} if schema.get("pii_columns") else {}
                body, _ = mask_table(header, t["rows"], p.unmask, **kw)
                if c["kind"] == "table":
                    return render_whole(header, body), True
                m = re.search(r"Rows (\d+)-(\d+)", c["section_path"][-1] if c["section_path"] else "")
                if m:
                    return render_rows(header, body[int(m.group(1)) - 1 : int(m.group(2))]), True
        txt, cats = mask_text(c["text"], p.unmask)
        return txt, bool(cats)


def _no_stricter(current: Access, new: Access) -> bool:
    """True when ``new`` adds no restriction that ``current`` lacks."""
    from .models import class_rank

    if class_rank(new.doc_class) > class_rank(current.doc_class):
        return False
    if "*" not in new.divisions and ("*" in current.divisions or not set(current.divisions) <= set(new.divisions)):
        return False
    if new.projects and (not current.projects or not set(current.projects) <= set(new.projects)):
        return False
    return not (new.groups and not set(new.groups) <= set(current.groups))


def mentions_clause(text: str, clause: str) -> bool:
    """'16.1' as a whole clause number: not inside '116.1', '16.12' or a date."""
    return bool(re.search(rf"(?<![\d.]){re.escape(clause)}(?!\d|\.\d)", text or ""))


def _clause_match(a: str, b: str) -> bool:
    return a == b or b.startswith(a + ".") or a.startswith(b + ".")


def _majority(xs: list[str]) -> str:
    return max(set(xs), key=xs.count) if xs else "en"


TRUST_LEVELS = ("pack", "curator", "content")
# front-matter keys a file may only set about itself when it comes from a tenant pack
PROTECTED_FRONT_MATTER = frozenset(
    {"doc_id", "access", "supersedes", "superseded_by", "amends", "modifies", "relations", "legal_status", "status"}
)
_DOC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def doc_id_ok(doc_id: str) -> bool:
    return bool(_DOC_ID.fullmatch(doc_id or ""))


def _may_version(existing: dict[str, Any], actor: str, source: str | None, named: bool) -> bool:
    """May a content-trust file become the next version of ``existing``?

    Yes when the same person uploaded it, or the same connector (a re-crawl of the
    same URL or record) produced it. A legacy document with no recorded owner can
    only be versioned by its own connector."""
    meta = existing.get("meta") or {}
    if source and source.startswith("connector:"):
        return existing.get("source") == source
    if meta.get("owner"):
        return meta["owner"] == actor and (existing.get("source") or "upload") == (source or "upload")
    return named and existing.get("source") == source and bool(source)


def _derive_doc_id(source: str | None, url: str | None, ref: str | None, filename: str) -> str:
    if url:
        from .watch import normalise_url

        return "url-" + text_hash(normalise_url(url))[:16]
    if ref:
        return "ref-" + re.sub(r"[^a-z0-9]+", "-", ref.lower()).strip("-")[:80]
    stem = Path(filename).stem
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:80] or "doc-" + text_hash(filename)[:12]


def scan_malware(data: bytes) -> dict[str, Any]:
    """ClamAV INSTREAM scan when SMRITI_CLAMD=host:port is set; otherwise a no-op
    that says so (recorded in the audit trail by the caller's context)."""
    import os

    target = os.environ.get("SMRITI_CLAMD")
    if not target:
        return {"scanned": False}
    host, _, port = target.partition(":")
    try:
        with socket.create_connection((host, int(port or 3310)), timeout=10) as s:
            s.sendall(b"zINSTREAM\0")
            for i in range(0, len(data), 8192):
                part = data[i : i + 8192]
                s.sendall(struct.pack("!L", len(part)) + part)
            s.sendall(struct.pack("!L", 0))
            reply = s.recv(4096).decode("utf-8", "replace").strip("\0\n ")
    except OSError as e:
        return {"scanned": False, "error": str(e)}
    if not reply or reply.endswith("ERROR"):  # e.g. "INSTREAM size limit exceeded. ERROR": not scanned, not clean
        return {"scanned": False, "error": reply or "empty reply from clamd"}
    infected = reply.endswith("FOUND")
    return {
        "scanned": True,
        "infected": infected,
        "signature": reply.split(":", 1)[-1].replace("FOUND", "").strip() if infected else None,
    }
