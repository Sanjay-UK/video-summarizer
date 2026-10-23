#!/usr/bin/env python3
"""Lightweight HTTP API on RunPod port 8765 for heavy pipeline (SSH-free transport)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

APP_ROOT = Path(os.environ.get("RUNPOD_REMOTE_APP_ROOT", "/workspace/video-summarizer"))
JOBS_ROOT = Path(os.environ.get("RUNPOD_REMOTE_JOBS_ROOT", "/workspace/vsumm-jobs"))
HOST = os.environ.get("VSUMM_HTTP_BIND", "0.0.0.0")
PORT = int(os.environ.get("VSUMM_HTTP_PORT", "8765"))


class Handler(BaseHTTPRequestHandler):
    server_version = "vsumm-http/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if path.startswith("/jobs/") and path.endswith("/status"):
            job_id = path.split("/")[2]
            done = (JOBS_ROOT / job_id / "heavy_done.json").is_file()
            log_path = JOBS_ROOT / job_id / "heavy_worker.log"
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.is_file() else ""
            self._json(200, {"job_id": job_id, "done": done, "log_tail": tail})
            return
        if path.startswith("/jobs/") and "/files/" in path:
            parts = path.split("/")
            if len(parts) >= 5 and parts[3] == "files":
                job_id, name = parts[2], unquote("/".join(parts[4:]))
                file_path = JOBS_ROOT / job_id / name
                if not file_path.is_file():
                    self.send_error(404)
                    return
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/sync":
            data = self._read_body()
            APP_ROOT.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                with tarfile.open(tmp_path, "r:gz") as tar:
                    tar.extractall(path=APP_ROOT.parent)
                (APP_ROOT / ".vsumm_sync_ok").touch()
            finally:
                Path(tmp_path).unlink(missing_ok=True)
            self._json(200, {"ok": True})
            return

        if path.startswith("/jobs/") and path.endswith("/run"):
            job_id = path.split("/")[2]
            work = JOBS_ROOT / job_id
            work.mkdir(parents=True, exist_ok=True)
            manifest = json.loads(self._read_body().decode("utf-8"))
            (work / "heavy_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            def _run() -> None:
                log_path = work / "heavy_worker.log"
                with log_path.open("w", encoding="utf-8") as log:
                    subprocess.run(
                        [
                            sys.executable,
                            str(APP_ROOT / "scripts/runpod_heavy_worker.py"),
                            "--workdir",
                            str(work),
                        ],
                        cwd=str(APP_ROOT),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )

            threading.Thread(target=_run, daemon=True).start()
            self._json(202, {"started": True, "job_id": job_id})
            return

        if path.startswith("/jobs/") and path.endswith("/upload"):
            job_id = path.split("/")[2]
            work = JOBS_ROOT / job_id
            work.mkdir(parents=True, exist_ok=True)
            name = self.headers.get("X-Filename", "audio.mp3")
            (work / name).write_bytes(self._read_body())
            self._json(200, {"ok": True, "file": name})
            return

        self.send_error(404)


def main() -> None:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"vsumm HTTP listening on {HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
