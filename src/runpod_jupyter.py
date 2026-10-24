"""RunPod pod control via Jupyter HTTP proxy (when SSH proxy is unreachable)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import struct
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from src.gpu_selection import load_env_file

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) video-summarizer/1.0",
    "Accept": "application/json",
}


def _cookie_header(jar: CookieJar) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in jar)


def _ws_recv_frame(sock: socket.socket) -> bytes:
    header = sock.recv(2)
    if len(header) < 2:
        return b""
    length = header[1] & 127
    if length == 126:
        length = struct.unpack(">H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", sock.recv(8))[0]
    mask = header[1] & 128
    if mask:
        mask_key = sock.recv(4)
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk
    if mask:
        data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
    return data


def _ws_send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    frame = bytearray([0x81])
    length = len(payload)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(127)
        frame.extend(struct.pack(">Q", length))
    frame.extend(payload)
    sock.sendall(frame)


def _jupyter_ws_execute(
    *,
    base: str,
    kernel_id: str,
    code: str,
    cookies: str,
    timeout: float,
) -> None:
    session_id = secrets.token_hex(16)
    ws_path = f"/api/kernels/{kernel_id}/channels?session_id={session_id}"
    host = urllib.parse.urlparse(base).netloc
    key = secrets.token_bytes(16)
    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
    req = (
        f"GET {ws_path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(key).decode()}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Cookie: {cookies}\r\n"
        f"Origin: {base}\r\n\r\n"
    ).encode()
    ctx = ssl.create_default_context()
    raw_sock = socket.create_connection((host, 443), timeout=30)
    sock = ctx.wrap_socket(raw_sock, server_hostname=host)
    sock.sendall(req)
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)
    if b" 101 " not in resp:
        raise RuntimeError(f"Jupyter websocket upgrade failed: {resp[:300]!r}")

    msg_id = secrets.token_hex(16)
    message = {
        "header": {
            "msg_id": msg_id,
            "username": "",
            "session": session_id,
            "msg_type": "execute_request",
            "version": "5.3",
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": code,
            "silent": False,
            "store_history": False,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
        "channel": "shell",
        "buffers": [],
    }
    _ws_send_text(sock, json.dumps(message))
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = _ws_recv_frame(sock)
        if not frame:
            continue
        try:
            payload = json.loads(frame.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        parent = payload.get("parent_header") or {}
        if parent.get("msg_id") != msg_id:
            continue
        msg_type = payload.get("msg_type")
        if msg_type == "error":
            content = payload.get("content") or {}
            raise RuntimeError(
                f"Jupyter execute error: {content.get('ename')}: {content.get('evalue')}"
            )
        if msg_type == "execute_reply":
            content = payload.get("content") or {}
            if content.get("status") == "error":
                raise RuntimeError(f"Jupyter execute failed: {content}")
            if content.get("status") == "ok":
                return
    raise RuntimeError("Jupyter websocket execute timed out")


def _pod_id_from_env_or_deploy() -> str:
    from src.compute_status import get_compute_deploy_status

    deploy = get_compute_deploy_status(refresh_live=False)
    pod_id = (deploy.get("pod_id") or os.environ.get("RUNPOD_POD_ID") or "").strip()
    if not pod_id:
        deploy = get_compute_deploy_status(refresh_live=True)
        pod_id = (deploy.get("pod_id") or "").strip()
    if not pod_id or not re.fullmatch(r"^[a-zA-Z0-9_-]{1,64}$", pod_id):
        raise RuntimeError("No RunPod pod_id for Jupyter transport.")
    return pod_id


def _fetch_jupyter_password(pod_id: str, api_key: str) -> str:
    saved = os.environ.get("RUNPOD_JUPYTER_PASSWORD", "").strip()
    if saved:
        return saved

    url = f"https://api.runpod.io/v2/pods/{pod_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"RunPod pod fetch failed: HTTP {exc.code}") from exc

    match = re.search(r'"JUPYTER_PASSWORD"\s*:\s*"([^"\\]+)"', raw)
    if not match:
        raise RuntimeError(
            "Jupyter password not found. Deploy with startJupyter: true or set "
            "RUNPOD_JUPYTER_PASSWORD in .env."
        )
    return match.group(1)


def jupyter_base_url(pod_id: str, port: int = 8888) -> str:
    port = int(os.environ.get("RUNPOD_JUPYTER_PORT", str(port)))
    return f"https://{pod_id}-{port}.proxy.runpod.net"


class RunpodJupyterClient:
    def __init__(self, pod_id: str, password: str, *, port: int = 8888) -> None:
        self.pod_id = pod_id
        self.base = jupyter_base_url(pod_id, port)
        self._password = password
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._kernel_id: str | None = None
        self._xsrf: str = ""

    def _xsrf_header(self) -> dict[str, str]:
        if not self._xsrf:
            for cookie in self._jar:
                if cookie.name == "_xsrf":
                    self._xsrf = cookie.value
                    break
        if self._xsrf:
            return {"X-XSRFToken": self._xsrf}
        return {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> bytes:
        hdrs = {**_BROWSER_HEADERS, **self._xsrf_header(), **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"Jupyter {method} {path} failed HTTP {exc.code}: {body}") from exc

    def login(self) -> None:
        html = self._request("GET", "/login", headers={"Accept": "text/html"}).decode()
        xsrf_match = re.search(r'name="_xsrf"\s+value="([^"]+)"', html)
        xsrf = xsrf_match.group(1) if xsrf_match else ""
        form = urllib.parse.urlencode({"password": self._password, "_xsrf": xsrf}).encode()
        self._request(
            "POST",
            "/login",
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.base + "/login",
                "Accept": "text/html",
            },
        )
        self._xsrf = ""
        _ = self._xsrf_header()

    def ensure_kernel(self) -> str:
        if self._kernel_id:
            return self._kernel_id
        body = json.dumps({"name": "python3"}).encode()
        raw = self._request(
            "POST",
            "/api/kernels",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        payload = json.loads(raw.decode())
        self._kernel_id = payload["id"]
        return self._kernel_id

    def execute(self, code: str, *, timeout: float = 600) -> None:
        kid = self.ensure_kernel()
        _jupyter_ws_execute(
            base=self.base,
            kernel_id=kid,
            code=code,
            cookies=_cookie_header(self._jar),
            timeout=timeout,
        )

    def upload_file(self, remote_path: str, local_path: Path) -> None:
        remote_path = remote_path.lstrip("/")
        content_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")
        body = json.dumps(
            {
                "type": "file",
                "format": "base64",
                "content": content_b64,
            }
        ).encode()
        encoded = urllib.parse.quote(remote_path, safe="/")
        self._request(
            "PUT",
            f"/api/contents/{encoded}",
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=max(600, local_path.stat().st_size // 5000),
        )

    def download_file(self, remote_path: str, local_path: Path) -> None:
        remote_path = remote_path.lstrip("/")
        encoded = urllib.parse.quote(remote_path, safe="/")
        raw = self._request("GET", f"/api/contents/{encoded}?content=1", timeout=600)
        payload = json.loads(raw.decode())
        fmt = payload.get("format")
        content = payload.get("content")
        if content is None:
            raise RuntimeError(f"Jupyter contents missing body for {remote_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "base64":
            local_path.write_bytes(base64.b64decode(content))
        elif fmt == "text":
            local_path.write_text(str(content), encoding="utf-8")
        else:
            raise RuntimeError(f"Unexpected contents format {fmt!r} for {remote_path}")

    def remote_file_exists(self, remote_path: str) -> bool:
        remote_path = remote_path.lstrip("/")
        encoded = urllib.parse.quote(remote_path, safe="/")
        try:
            self._request("GET", f"/api/contents/{encoded}", timeout=60)
            return True
        except RuntimeError as exc:
            if "404" in str(exc):
                return False
            raise


def build_jupyter_client() -> RunpodJupyterClient:
    load_env_file()
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RUNPOD_API_KEY missing for Jupyter transport.")
    pod_id = _pod_id_from_env_or_deploy()
    password = _fetch_jupyter_password(pod_id, api_key)
    client = RunpodJupyterClient(pod_id, password)
    client.login()
    return client


def probe_jupyter_reachable() -> None:
    build_jupyter_client()


def sync_project_via_jupyter(client: RunpodJupyterClient, project_dir: Path) -> None:
    marker = "video-summarizer/.vsumm_sync_ok"
    if client.remote_file_exists(marker):
        return

    includes = [
        "config.py",
        "scripts/runpod_heavy_worker.py",
        "scripts/runpod_nllb_batch.py",
        "src",
    ]
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in includes:
            path = project_dir / name
            if path.is_file():
                tar.add(path, arcname=f"video-summarizer/{name}")
            elif path.is_dir():
                tar.add(path, arcname=f"video-summarizer/{name}")

    tmp = project_dir / ".vsumm_bundle.tgz"
    tmp.write_bytes(buf.getvalue())
    try:
        client.upload_file("vsumm_bundle.tgz", tmp)
        client.execute(
        "import tarfile, pathlib\n"
        "p = pathlib.Path('vsumm_bundle.tgz')\n"
        "tarfile.open(p).extractall('.')\n"
        "pathlib.Path('video-summarizer/.vsumm_sync_ok').touch()\n"
        "p.unlink(missing_ok=True)\n"
        "print('bundle_ok')",
        timeout=600,
    )
    finally:
        tmp.unlink(missing_ok=True)


def bootstrap_runpod_heavy_environment(client: RunpodJupyterClient) -> None:
    """Ensure faster-whisper is importable on the pod (once per pod)."""
    marker = "video-summarizer/.vsumm_heavy_bootstrap_ok"
    if client.remote_file_exists(marker):
        return
    client.execute(
        "import pathlib, subprocess, sys\n"
        "subprocess.run(\n"
        "    [sys.executable, '-m', 'pip', 'install', '--root-user-action=ignore', 'faster-whisper'],\n"
        "    check=False,\n"
        ")\n"
        "from faster_whisper import WhisperModel  # noqa: F401\n"
        "pathlib.Path('video-summarizer/.vsumm_heavy_bootstrap_ok').touch()\n"
        "print('bootstrap_ok')\n",
        timeout=900,
    )


def run_heavy_phase_via_jupyter(
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
    client = build_jupyter_client()
    log("RunPod transport: Jupyter HTTP proxy (SSH unavailable from this network)")
    project_dir = Path(__file__).resolve().parent.parent
    sync_project_via_jupyter(client, project_dir=project_dir)
    bootstrap_runpod_heavy_environment(client)

    remote_root = os.environ.get("RUNPOD_REMOTE_JOBS_ROOT", "vsumm-jobs").strip().strip("/")
    remote_rel = f"{remote_root}/{job_id}"

    client.execute(
        f"import pathlib\npathlib.Path('{remote_rel}').mkdir(parents=True, exist_ok=True)\n"
        "print('mkdir_ok')",
        timeout=120,
    )

    manifest = {
        "job_id": job_id,
        "audio_file": audio_path.name,
        "whisper_model": whisper_model,
        "summarization_model": summarization_model,
        "compression": compression,
        "highlight_languages": highlight_languages,
    }
    manifest_local = job_output_dir / "heavy_manifest.json"
    manifest_local.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    client.upload_file(f"{remote_rel}/{audio_path.name}", audio_path)
    client.upload_file(f"{remote_rel}/heavy_manifest.json", manifest_local)

    done_rel = f"{remote_rel}/heavy_done.json"
    log_path = f"{remote_rel}/heavy_worker.log"
    app_root_override = os.environ.get("RUNPOD_REMOTE_APP_ROOT", "").strip().strip("/")

    start_code = (
        "import os, subprocess, pathlib, sys\n"
        f"override = {app_root_override!r}\n"
        "candidates = []\n"
        "if override:\n"
        "    candidates.append(pathlib.Path(override))\n"
        "candidates.extend([\n"
        "    pathlib.Path('/workspace/video-summarizer'),\n"
        "    pathlib.Path('video-summarizer'),\n"
        "])\n"
        "app = None\n"
        "for cand in candidates:\n"
        "    try:\n"
        "        resolved = cand.expanduser().resolve()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if (resolved / 'scripts/runpod_heavy_worker.py').is_file():\n"
        "        app = resolved\n"
        "        break\n"
        "if app is None:\n"
        "    raise SystemExit('runpod_heavy_worker.py not found under /workspace/video-summarizer or video-summarizer')\n"
        f"work = pathlib.Path('{remote_rel}').resolve()\n"
        f"log = open('{log_path}', 'w')\n"
        "worker = app / 'scripts/runpod_heavy_worker.py'\n"
        "env = {**os.environ, 'RUNPOD_WORKER_TRANSCRIBE_ONLY': '1', 'WHISPER_DEVICE': 'cuda'}\n"
        "proc = subprocess.Popen(\n"
        "    [sys.executable, str(worker), '--workdir', str(work)],\n"
        "    stdout=log, stderr=subprocess.STDOUT, cwd=str(app), env=env,\n"
        ")\n"
        "print('started', proc.pid, 'app', app)\n"
    )
    client.execute(start_code, timeout=120)
    log(f"RunPod Jupyter: heavy worker started (log {log_path})")

    deadline = time.time() + float(os.environ.get("RUNPOD_HEAVY_TIMEOUT_SEC", "7200"))
    while time.time() < deadline:
        if client.remote_file_exists(done_rel):
            break
        if client.remote_file_exists(log_path):
            try:
                client.download_file(log_path, job_output_dir / "_heavy_worker.log")
                tail = (job_output_dir / "_heavy_worker.log").read_text(
                    encoding="utf-8", errors="replace"
                )
                if any(
                    needle in tail
                    for needle in (
                        "can't open file",
                        "No such file or directory",
                        "Missing audio",
                        "Missing ",
                        "[ERROR] ollama",
                        "faster-whisper not installed",
                        "Traceback (most recent call last)",
                    )
                ):
                    snippet = tail.strip()[-800:]
                    raise RuntimeError(
                        f"RunPod heavy worker failed on pod (see heavy_worker.log): {snippet}"
                    )
            except RuntimeError:
                raise
            except OSError:
                pass
        time.sleep(20)
        log("RunPod Jupyter: waiting for GPU heavy worker…")
    else:
        raise RuntimeError("RunPod heavy worker timed out (no heavy_done.json).")

    for name in ("transcript.txt", "segments.json", "heavy_done.json"):
        client.download_file(f"{remote_rel}/{name}", job_output_dir / name)

    if not (job_output_dir / "transcript.txt").is_file():
        raise RuntimeError("Jupyter transport: heavy worker did not produce transcript.txt")

    from src.locales import parse_language_list
    from src.summary_translate import translate_summary_for_languages
    from src.summarizer import summarize_transcript

    log("RunPod Jupyter: transcribe done on GPU; summarizing locally (Ollama on this machine)")
    summarize_transcript(
        transcript_path=job_output_dir / "transcript.txt",
        segments_path=job_output_dir / "segments.json",
        output_dir=job_output_dir,
        model=summarization_model,
        compression=compression,
    )
    summary_text = (job_output_dir / "summary.txt").read_text(encoding="utf-8")
    langs = parse_language_list(highlight_languages)
    translate_summary_for_languages(job_output_dir, summary_text, langs)
