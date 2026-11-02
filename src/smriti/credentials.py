"""Connector credentials, encrypted at rest.

Tokens, passwords and database URLs entered in the UI are stored with Fernet
(AES-128-CBC + HMAC-SHA256). The key comes from SMRITI_SECRET_KEY, or is
generated once into <data_dir>/secret.key. The API never returns stored
values: the UI only learns which secret fields are set. In production, keep
the key in OpenBao or your KMS and pass it through the environment.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, data_dir: Path):
        key = os.environ.get("SMRITI_SECRET_KEY", "").encode()
        if not key:
            f = Path(data_dir) / "secret.key"
            if f.exists():
                key = f.read_bytes().strip()
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                key = Fernet.generate_key()
                f.write_bytes(key)
                with contextlib.suppress(OSError):  # not supported on every filesystem
                    f.chmod(0o600)
        self.f = Fernet(key)

    def seal(self, values: dict[str, Any]) -> str:
        clean = {k: v for k, v in values.items() if v not in (None, "")}
        return self.f.encrypt(json.dumps(clean).encode()).decode() if clean else ""

    def open(self, token: str | None) -> dict[str, Any]:
        if not token:
            return {}
        try:
            return json.loads(self.f.decrypt(token.encode()))
        except InvalidToken as e:
            raise RuntimeError("stored connector secrets cannot be decrypted: SMRITI_SECRET_KEY changed") from e
