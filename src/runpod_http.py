"""RunPod heavy phase over HTTPS pod proxy (port 8765), no SSH."""

from __future__ import annotations

import json
import os
import tarfile
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Callable

from src.gpu_selection import load_env_file
from src.runpod_jupyter import _pod_id_from_env_or_deploy, build_jupyter_client


def _http_base(pod_id: str) -> str:
    port = int(os.environ.get("RUNPOD_HTTP_PORT", "8765"))
    return f"https://{pod_id}-{port}.proxy.runpod.net"


def _http_request(method: str, url: str, data: bytes | None = None, *, timeout: float = 600) -> bytes:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) video-summarizer/1.0",
    )
    if data is not None:
        req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"RunPod HTTP {method} {url} failed: HTTP {exc.code} {body}") from exc


def _restart_pod_with_http_server(pod_id: str, api_key: str) -> None:
    patch_url = f"https://api.runpod.io/v2/pods/{pod_id}"
    body = json.dumps(
        {
            "args": "python3 /workspace/vsumm_http_server.py"
        }
    ).encode()
    req = urllib.request.Request(
        patch_url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        resp.read()
    action_url = f"https://api.runpod.io/v2/pods/{pod_id}/action"
    action_body = json.dumps({"action": "restart"}).encode()
    req2 = urllib.request.Request(
        action_url,
        data=action_body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req2, timeout=90) as resp:
        resp.read()


def ensure_http_worker_running(pod_id: str) -> None:
    base = _http_base(pod_id)
    try:
        _http_request("GET", f"{base}/health", timeout=30)
        return
    except RuntimeError:
        pass

    load_env_file()
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RUNPOD_API_KEY missing for HTTP worker bootstrap.")

    client = build_jupyter_client()
    server_path = Path(__file__).resolve().parent.parent / "scripts/runpod_heavy_http_server.py"
    client.upload_file("vsumm_http_server.py", server_path)
    client.execute(
        "import subprocess, sys, os\n"
        "subprocess.Popen(\n"
        "    [sys.executable, '/workspace/vsumm_http_server.py'],\n"
        "    stdout=open('/workspace/vsumm_http.log', 'a'),\n"
        "    stderr=subprocess.STDOUT,\n"
        "    env={**os.environ, 'VSUMM_HTTP_PORT': '8765', 'VSUMM_HTTP_BIND': '0.0.0.0'},\n"
        ")\n"
        "print('vsumm_http_started')\n",
        timeout=120,
    )

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            _http_request("GET", f"{base}/health", timeout=15)
            return
        except RuntimeError:
            time.sleep(5)
    raise RuntimeError(
        f"RunPod HTTP worker did not become healthy at {base}/health after pod restart."
    )


def probe_http_reachable() -> None:
    pod_id = _pod_id_from_env_or_deploy()
    _http_request("GET", f"{_http_base(pod_id)}/health", timeout=30)


def run_heavy_phase_via_http(
    *,
    job_id: str,
    audio_path: Path,
    job_output_dir: Path,
    whisper_model: str,
    summarization_model: str,
    compression: str,
    highlight_languages: str,
    log: Callable[[str], None],
) -> None:
    load_env_file()
    pod_id = _pod_id_from_env_or_deploy()
    base = _http_base(pod_id)

    log("RunPod transport: HTTPS proxy on port 8765 (SSH unavailable)")
    ensure_http_worker_running(pod_id)

    buf = BytesIO()
    includes = [
        "config.py",
        "scripts/runpod_heavy_worker.py",
        "scripts/runpod_nllb_batch.py",
        "scripts/runpod_heavy_http_server.py",
        "src",
    ]
    project_dir = Path(__file__).resolve().parent.parent
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in includes:
            path = project_dir / name
            if path.is_file():
                tar.add(path, arcname=f"video-summarizer/{name}")
            elif path.is_dir():
                tar.add(path, arcname=f"video-summarizer/{name}")
    _http_request("POST", f"{base}/sync", data=buf.getvalue(), timeout=900)

    manifest = {
        "job_id": job_id,
        "audio_file": audio_path.name,
        "whisper_model": whisper_model,
        "summarization_model": summarization_model,
        "compression": compression,
        "highlight_languages": highlight_languages,
    }
    audio_bytes = audio_path.read_bytes()
    upload_url = f"{base}/jobs/{job_id}/upload"
    req = urllib.request.Request(upload_url, data=audio_bytes, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("X-Filename", audio_path.name)
    with urllib.request.urlopen(req, timeout=600) as resp:
        resp.read()

    _http_request(
        "POST",
        f"{base}/jobs/{job_id}/run",
        data=json.dumps(manifest).encode("utf-8"),
        timeout=60,
    )
    log(f"RunPod HTTP: heavy worker started for job {job_id}")

    deadline = time.time() + float(os.environ.get("RUNPOD_HEAVY_TIMEOUT_SEC", "7200"))
    while time.time() < deadline:
        status = json.loads(_http_request("GET", f"{base}/jobs/{job_id}/status", timeout=60).decode())
        if status.get("done"):
            break
        time.sleep(15)
        log("RunPod HTTP: waiting for GPU heavy worker…")
    else:
        raise RuntimeError("RunPod heavy worker timed out.")

    for name in (
        "transcript.txt",
        "segments.json",
        "summary.txt",
        "summary_translations.json",
        "heavy_done.json",
    ):
        raw = _http_request("GET", f"{base}/jobs/{job_id}/files/{name}", timeout=600)
        (job_output_dir / name).write_bytes(raw)

    if not (job_output_dir / "transcript.txt").is_file():
        raise RuntimeError("HTTP transport: missing transcript.txt from pod")
