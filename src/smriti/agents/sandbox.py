"""Code execution for agents, isolated from Smriti's data and network.

Production runs ``smriti sandbox`` in its own container: no route to the
internet or to Postgres, Qdrant and the model servers (an internal-only Docker
network shared with the API), a read-only root filesystem, no capabilities, a
non-root user, and memory, process and time limits. The API reaches it at
``SMRITI_SANDBOX_URL``.

``SMRITI_SANDBOX=local`` runs code in a subprocess of the API instead. It has
the same time and memory limits on Linux but shares the host's network and
filesystem, so it is for development only.

Files the code writes to its working directory come back as artifacts.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

MAX_OUTPUT = 20_000
MAX_FILES = 5
MAX_FILE_BYTES = 5 * 1024 * 1024


class SandboxUnavailable(RuntimeError):
    pass


def mode() -> str:
    if os.environ.get("SMRITI_SANDBOX_URL"):
        return "service"
    return "local" if os.environ.get("SMRITI_SANDBOX") == "local" else "off"


def run(code: str, timeout_s: float = 15.0) -> dict[str, Any]:
    m = mode()
    if m == "service":
        headers = (
            {"Authorization": f"Bearer {os.environ['SMRITI_SANDBOX_TOKEN']}"}
            if os.environ.get("SMRITI_SANDBOX_TOKEN")
            else {}
        )
        try:
            r = httpx.post(
                os.environ["SMRITI_SANDBOX_URL"].rstrip("/") + "/run",
                json={"code": code, "timeout_s": timeout_s},
                headers=headers,
                timeout=timeout_s + 10,
            )
            r.raise_for_status()
            return dict(r.json())
        except httpx.HTTPError as e:
            raise SandboxUnavailable(f"sandbox service: {e}") from e
    if m == "local":
        return run_local(code, timeout_s)
    raise SandboxUnavailable("code execution is off: start the sandbox service (SMRITI_SANDBOX_URL)")


def _limits(mem_mb: int, cpu_s: int):  # type: ignore[no-untyped-def]
    def apply() -> None:  # runs in the child before exec (POSIX only)
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES * 2, MAX_FILE_BYTES * 2))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        os.setsid()

    return apply


def run_local(code: str, timeout_s: float = 15.0, mem_mb: int = 512) -> dict[str, Any]:
    timeout_s = max(1.0, min(float(timeout_s), 60.0))
    with tempfile.TemporaryDirectory(prefix="smriti-sbx-") as d:
        work = Path(d)
        (work / "main.py").write_text(code, encoding="utf-8")
        env = {"PATH": os.defpath, "HOME": d, "TMPDIR": d, "PYTHONIOENCODING": "utf-8", "MPLBACKEND": "Agg"}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
        kw: dict[str, Any] = {}
        if os.name == "posix":
            kw["preexec_fn"] = _limits(mem_mb, int(timeout_s) + 1)
        timed_out = False
        try:
            p = subprocess.run(
                [sys.executable, "-I", "main.py"],
                cwd=d,
                env=env,
                capture_output=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                check=False,
                **kw,
            )
            out, err, code_ = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, err, code_, timed_out = e.stdout or b"", e.stderr or b"", -9, True
        files = []
        for f in sorted(work.iterdir()):
            if f.name == "main.py" or not f.is_file() or len(files) >= MAX_FILES:
                continue
            data = f.read_bytes()[:MAX_FILE_BYTES]
            files.append({"name": f.name, "size": len(data), "b64": base64.b64encode(data).decode()})
    return {
        "stdout": out.decode("utf-8", "replace")[-MAX_OUTPUT:],
        "stderr": err.decode("utf-8", "replace")[-MAX_OUTPUT:],
        "exit_code": code_,
        "timed_out": timed_out,
        "files": files,
    }


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._send(200, {"ok": True}) if self.path == "/health" else self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        token = os.environ.get("SMRITI_SANDBOX_TOKEN")
        if token and self.headers.get("Authorization") != f"Bearer {token}":
            return self._send(401, {"error": "bad token"})
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self._send(413, {"error": "code too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, run_local(str(body.get("code", "")), float(body.get("timeout_s", 15))))
        except (ValueError, OSError) as e:
            self._send(400, {"error": str(e)})

    def _send(self, status: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: Any) -> None:  # code is not logged
        pass


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:  # noqa: S104 - inside its own isolated network
    print(f"smriti sandbox on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), _Handler).serve_forever()
