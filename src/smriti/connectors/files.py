"""File connectors: browser uploads and server folders (shared drives)."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator
from pathlib import Path

from .base import COMMON_FIELDS, Connector, ConnectorError, F, Item


class UploadConnector(Connector):
    """Files uploaded through the browser. They are stored in the blob store
    first, so the upload returns at once and the worker parses them."""

    kind = "upload"
    label = "Upload files"
    description = (
        "PDF, Word, Excel, PowerPoint-exported PDF, HTML, email, CSV, Markdown or ZIP archives, from your computer."
    )
    icon = "⇪"
    fields = list(COMMON_FIELDS)
    uses_network = False

    def discover(self) -> Iterator[Item]:
        blobs = self.ctx.tenant.blobs
        for f in self.ctx.config.get("files", []):
            key, name = f["blob_key"], f["filename"]
            yield Item(key=key, filename=name, fetch=lambda k=key: blobs.get(k), title=name, size=f.get("size"))


EXTRA_ROOTS: list[Path] = []  # <data dir>/imports of each Platform in this process (demo files)


def folder_roots() -> list[Path]:
    """Folders a connector may read: SMRITI_FOLDER_ROOTS (default /imports, the
    Docker mount) plus each data directory's imports/ folder."""
    roots = [
        Path(p.strip()).resolve() for p in os.environ.get("SMRITI_FOLDER_ROOTS", "/imports").split(",") if p.strip()
    ]
    return roots + [r for r in EXTRA_ROOTS if r not in roots]


class FolderConnector(Connector):
    kind = "folder"
    label = "Server folder / shared drive"
    description = "A folder mounted on the Smriti server (SMB/NFS share, synced drive). Only folders under SMRITI_FOLDER_ROOTS are allowed."
    icon = "🗂"
    fields = [
        F("path", "Folder", required=True, placeholder="/imports/circulars", help="must be under an allowed root"),
        F("pattern", "File pattern", default="*", help="e.g. *.pdf"),
        F("recursive", "Include subfolders", "bool", default=True),
        F("max_file_mb", "Max file size (MB)", "number", default=100),
        *COMMON_FIELDS,
    ]
    uses_network = False

    def discover(self) -> Iterator[Item]:
        root = Path(self.cfg["path"]).resolve()
        roots = folder_roots()
        if not any(r == root or r in root.parents for r in roots):
            raise ConnectorError(f"folder must be under {', '.join(map(str, roots))} (SMRITI_FOLDER_ROOTS)")
        if not root.is_dir():
            raise ConnectorError(f"not a folder: {root}")
        it = root.rglob("*") if self.cfg.get("recursive") else root.glob("*")
        limit = int(self.cfg.get("max_file_mb") or 100) * 1024 * 1024
        for p in sorted(it):
            if not p.is_file() or p.name.startswith(".") or p.name.endswith(".meta.yaml"):
                continue
            if not fnmatch.fnmatch(p.name.lower(), (self.cfg.get("pattern") or "*").lower()):
                continue
            if p.stat().st_size > limit:
                self.ctx.log(f"skip {p.name}: larger than the size limit")
                continue
            rel = p.relative_to(root).as_posix()
            yield Item(key=rel, filename=p.name, fetch=lambda q=p: q.read_bytes(), title=p.stem, size=p.stat().st_size)
