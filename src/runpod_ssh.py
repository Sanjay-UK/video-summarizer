"""RunPod SSH/SCP helpers (proxy SSH command from deploy status)."""

from __future__ import annotations
from src.log_safety import redact

import os
import posixpath
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from src.compute_status import get_compute_deploy_status
from src.gpu_selection import load_env_file

_JOB_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SSH_CMD_RE = re.compile(
    r"^ssh\s+(?:(?:-i\s+\S+\s+))?(?P<user>[^@\s]+)@(?P<host>[^\s]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RunpodSshTarget:
    user: str
    host: str

    @property
    def spec(self) -> str:
        return f"{self.user}@{self.host}"


def _validate_job_id(job_id: str) -> str:
    job_id = (job_id or "").strip()
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid job_id")
    return job_id


def parse_ssh_command(command: str) -> RunpodSshTarget | None:
    command = (command or "").strip()
    if not command:
        return None
    match = _SSH_CMD_RE.match(command)
    if not match:
        return None
    return RunpodSshTarget(user=match.group("user"), host=match.group("host"))


def _prefer_direct_ssh() -> bool:
    raw = os.environ.get("RUNPOD_SSH_PREFER_DIRECT", "1").strip().lower()
    return raw not in ("0", "false", "no")


def _ssh_target_from_live_pod() -> tuple[RunpodSshTarget | None, RunpodSshTarget | None]:
    """Return (direct, proxy) SSH targets from live RunPod pod metadata."""
    from src.compute_status import _fetch_runpod_pod

    deploy = get_compute_deploy_status(refresh_live=False)
    pod_id = (deploy.get("pod_id") or "").strip()
    if not pod_id:
        return None, None
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        return None, None
    live, err = _fetch_runpod_pod(pod_id, api_key)
    if err or not live:
        return None, None
    ssh = live.get("ssh") or {}
    direct = parse_ssh_command((ssh.get("direct") or {}).get("command") or "")
    proxy = parse_ssh_command((ssh.get("proxy") or {}).get("command") or "")
    return direct, proxy


def get_runpod_ssh_target() -> RunpodSshTarget:
    load_env_file()
    override = os.environ.get("RUNPOD_SSH_TARGET", "").strip()
    if override and "@" in override:
        user, host = override.split("@", 1)
        return RunpodSshTarget(user=user.strip(), host=host.strip())

    deploy = get_compute_deploy_status(refresh_live=True)
    if deploy.get("phase") != "running":
        raise RuntimeError("No running RunPod pod for SSH.")

    direct, proxy = _ssh_target_from_live_pod()
    if _prefer_direct_ssh() and direct:
        return direct

    parsed = proxy or parse_ssh_command(deploy.get("ssh_command") or "")
    if not parsed:
        raise RuntimeError(
            "RunPod SSH command unavailable. Set RUNPOD_SSH_TARGET=user@ssh.runpod.io "
            "or refresh deploy status after pod start."
        )
    return parsed


def _ssh_identity_args() -> list[str]:
    identity = os.environ.get("RUNPOD_SSH_IDENTITY", "").strip()
    if identity:
        path = Path(identity).expanduser()
        if path.is_file():
            return ["-i", str(path)]
    return []


def _base_ssh(target: RunpodSshTarget) -> list[str]:
    return [
        "ssh",
        *_ssh_identity_args(),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        target.spec,
    ]


def _ssh_connect_timeout_sec() -> int:
    raw = os.environ.get("RUNPOD_SSH_CONNECT_TIMEOUT", "180").strip()
    try:
        return max(30, min(int(raw), 600))
    except ValueError:
        return 180


def _ssh_probe_timeout_sec() -> int:
    """Short timeout for reachability probes (fail fast before long sync retries)."""
    raw = os.environ.get("RUNPOD_SSH_PROBE_TIMEOUT", "25").strip()
    try:
        return max(8, min(int(raw), 120))
    except ValueError:
        return 25


def _ssh_retry_count() -> int:
    raw = os.environ.get("RUNPOD_SSH_RETRIES", "3").strip()
    try:
        return max(1, min(int(raw), 6))
    except ValueError:
        return 3


def _ssh_retry_delay_sec() -> float:
    raw = os.environ.get("RUNPOD_SSH_RETRY_DELAY", "5").strip()
    try:
        return max(1.0, min(float(raw), 30.0))
    except ValueError:
        return 5.0


def probe_runpod_ssh_reachable(*, target: RunpodSshTarget | None = None) -> None:
    """
    Fail fast if RunPod SSH proxy is unreachable (banner/auth/connect).
    Uses RUNPOD_SSH_PROBE_TIMEOUT (default 25s), single attempt.
    """
    load_env_file()
    resolved = target or get_runpod_ssh_target()
    identity = os.environ.get("RUNPOD_SSH_IDENTITY", "").strip()
    if identity:
        path = Path(identity).expanduser()
        if not path.is_file():
            raise RuntimeError(
                f"RUNPOD_SSH_IDENTITY points to missing file: {path}. "
                "Fix .env or switch to local mode on the home page."
            )
    else:
        raise RuntimeError(
            "RUNPOD_SSH_IDENTITY is not set. Add your RunPod private key path to .env "
            "or switch to local mode on the home page."
        )

    timeout = _ssh_probe_timeout_sec()
    cmd = _base_ssh(resolved) + ["echo", "vsumm_ssh_probe_ok"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            _format_ssh_failure(
                resolved,
                detail="SSH probe timed out before the pod responded.",
                timed_out=True,
            )
        ) from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 or "vsumm_ssh_probe_ok" not in stdout:
        detail = stderr or stdout or f"SSH probe exit {proc.returncode}"
        timed_out = "timed out" in detail.lower() or "banner exchange" in detail.lower()
        if "doesn't support pty" in detail.lower() or "doesn't support PTY" in detail:
            detail = (
                "RunPod proxy SSH connected but remote commands are unavailable "
                "(PTY/subsystem limitation). Use Jupyter transport or direct TCP SSH."
            )
        raise RuntimeError(
            _format_ssh_failure(
                resolved,
                detail=detail,
                timed_out=timed_out,
            )
        )


def get_runpod_ssh_probe_status() -> dict[str, str | bool | None]:
    """Non-fatal SSH probe for startup / status APIs."""
    load_env_file()
    try:
        target = get_runpod_ssh_target()
    except Exception as exc:
        return {"ok": False, "error": redact(str(exc)), "target": None}
    try:
        probe_runpod_ssh_reachable(target=target)
        return {"ok": True, "error": None, "target": target.spec}
    except RuntimeError as exc:
        return {"ok": False, "error": redact(str(exc)), "target": target.spec}


def _format_ssh_failure(
    target: RunpodSshTarget,
    *,
    detail: str,
    timed_out: bool = False,
) -> str:
    identity = os.environ.get("RUNPOD_SSH_IDENTITY", "").strip()
    hints = [
        f"Could not reach RunPod pod via SSH ({target.spec}).",
        "Add your public SSH key in the RunPod console and set RUNPOD_SSH_IDENTITY to the matching private key.",
        "Or set RUNPOD_SSH_TARGET=user@ssh.runpod.io if the deploy banner SSH user differs.",
        "You can retry the job or switch to local mode on the home page.",
    ]
    if timed_out:
        hints.insert(
            1,
            f"SSH timed out (RunPod proxy may be slow or the pod unreachable).",
        )
    body = detail.strip() if detail else ("SSH timed out." if timed_out else "SSH command failed.")
    return body + " — " + " ".join(hints)


def _run_subprocess_with_retries(
    cmd: list[str],
    *,
    timeout: int,
    input_bytes: bytes | None = None,
    target: RunpodSshTarget | None = None,
) -> subprocess.CompletedProcess[str]:
    last_exc: Exception | None = None
    attempts = _ssh_retry_count()
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_bytes,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt >= attempts:
                spec = target.spec if target else "RunPod pod"
                raise RuntimeError(
                    _format_ssh_failure(
                        target or RunpodSshTarget(user="pod", host="ssh.runpod.io"),
                        detail=f"SSH to {spec} timed out during connect/command.",
                        timed_out=True,
                    )
                ) from exc
        except OSError as exc:
            last_exc = exc
            if attempt >= attempts:
                raise RuntimeError(
                    _format_ssh_failure(
                        target or RunpodSshTarget(user="pod", host="ssh.runpod.io"),
                        detail=str(exc),
                    )
                ) from exc
        if attempt < attempts:
            time.sleep(_ssh_retry_delay_sec())
    raise RuntimeError(_format_ssh_failure(
        target or RunpodSshTarget(user="pod", host="ssh.runpod.io"),
        detail=str(last_exc or "SSH failed"),
    ))


def _base_scp(target: RunpodSshTarget) -> list[str]:
    return [
        "scp",
        *_ssh_identity_args(),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]


def ssh_run(target: RunpodSshTarget, remote_command: str, *, timeout: int = 7200) -> None:
    cmd = _base_ssh(target) + [remote_command]
    proc = _run_subprocess_with_retries(cmd, timeout=timeout, target=target)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            _format_ssh_failure(
                target,
                detail=f"exit {proc.returncode}: {detail[:1500]}",
            )
        )


def _runpod_proxy_ssh(target: RunpodSshTarget) -> bool:
    """RunPod's ssh.runpod.io proxy supports shell but not SCP/SFTP subsystems."""
    return target.host == "ssh.runpod.io"


def _transfer_timeout_sec(byte_count: int, *, floor: int = 120) -> int:
    return max(floor, min(7200, byte_count // 8000 + 60))


def _run_subprocess_binary_with_retries(
    cmd: list[str],
    *,
    timeout: int,
    input_bytes: bytes | None = None,
    target: RunpodSshTarget | None = None,
) -> subprocess.CompletedProcess[bytes]:
    last_exc: Exception | None = None
    attempts = _ssh_retry_count()
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                input=input_bytes,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt >= attempts:
                raise RuntimeError(
                    _format_ssh_failure(
                        target or RunpodSshTarget(user="pod", host="ssh.runpod.io"),
                        detail=f"SSH stream to {target.spec if target else 'pod'} timed out.",
                        timed_out=True,
                    )
                ) from exc
        except OSError as exc:
            last_exc = exc
            if attempt >= attempts:
                raise RuntimeError(
                    _format_ssh_failure(
                        target or RunpodSshTarget(user="pod", host="ssh.runpod.io"),
                        detail=str(exc),
                    )
                ) from exc
        if attempt < attempts:
            time.sleep(_ssh_retry_delay_sec())
    raise RuntimeError(
        _format_ssh_failure(
            target or RunpodSshTarget(user="pod", host="ssh.runpod.io"),
            detail=str(last_exc or "SSH stream failed"),
        )
    )


def ssh_stream_to_remote(target: RunpodSshTarget, local_path: Path, remote_path: str) -> None:
    """Upload a file over SSH stdin (works on RunPod proxy SSH without SCP subsystem)."""
    local_path = Path(local_path)
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    if ".." in remote_path or not remote_path.startswith("/"):
        raise ValueError("Invalid remote path")
    data = local_path.read_bytes()
    parent = posixpath.dirname(remote_path)
    quoted_parent = shlex.quote(parent)
    quoted_path = shlex.quote(remote_path)
    remote_cmd = f"mkdir -p {quoted_parent} && cat > {quoted_path}"
    cmd = _base_ssh(target) + [remote_cmd]
    timeout = _transfer_timeout_sec(len(data))
    proc = _run_subprocess_binary_with_retries(
        cmd, timeout=timeout, input_bytes=data, target=target
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
        raise RuntimeError(
            _format_ssh_failure(
                target,
                detail=f"SSH stream upload failed: {detail[:1200]}",
            )
        )


def ssh_stream_from_remote(target: RunpodSshTarget, remote_path: str, local_path: Path) -> None:
    """Download a file over SSH stdout (no SCP subsystem)."""
    if ".." in remote_path or not remote_path.startswith("/"):
        raise ValueError("Invalid remote path")
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    quoted_path = shlex.quote(remote_path)
    cmd = _base_ssh(target) + [f"cat {quoted_path}"]
    proc = _run_subprocess_binary_with_retries(cmd, timeout=7200, target=target)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
        raise RuntimeError(
            _format_ssh_failure(
                target,
                detail=f"SSH stream download failed: {detail[:1200]}",
            )
        )
    if not proc.stdout:
        raise RuntimeError(
            _format_ssh_failure(target, detail=f"SSH stream download empty for {remote_path}")
        )
    local_path.write_bytes(proc.stdout)


def _scp_subsystem_error(detail: str) -> bool:
    lowered = detail.lower()
    return "subsystem" in lowered or "scp:" in lowered


def copy_to_remote(target: RunpodSshTarget, local_path: Path, remote_path: str) -> None:
    """Upload using SCP on direct TCP SSH, or stdin streaming on RunPod proxy SSH."""
    if _runpod_proxy_ssh(target):
        ssh_stream_to_remote(target, local_path, remote_path)
        return
    try:
        scp_to_remote(target, local_path, remote_path)
    except RuntimeError as exc:
        if _scp_subsystem_error(str(exc)):
            ssh_stream_to_remote(target, local_path, remote_path)
            return
        raise


def copy_from_remote(target: RunpodSshTarget, remote_path: str, local_path: Path) -> None:
    if _runpod_proxy_ssh(target):
        ssh_stream_from_remote(target, remote_path, local_path)
        return
    try:
        scp_from_remote(target, remote_path, local_path)
    except RuntimeError as exc:
        if _scp_subsystem_error(str(exc)):
            ssh_stream_from_remote(target, remote_path, local_path)
            return
        raise


def scp_to_remote(target: RunpodSshTarget, local_path: Path, remote_path: str) -> None:
    local_path = Path(local_path)
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    if ".." in remote_path or not remote_path.startswith("/"):
        raise ValueError("Invalid remote path")
    dest = f"{target.spec}:{remote_path}"
    cmd = _base_scp(target) + [str(local_path), dest]
    proc = _run_subprocess_with_retries(cmd, timeout=600, target=target)
    if proc.returncode != 0:
        raise RuntimeError(
            _format_ssh_failure(
                target,
                detail=f"SCP upload failed: {(proc.stderr or proc.stdout)[:1200]}",
            )
        )


def scp_from_remote(target: RunpodSshTarget, remote_path: str, local_path: Path) -> None:
    if ".." in remote_path or not remote_path.startswith("/"):
        raise ValueError("Invalid remote path")
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    src = f"{target.spec}:{remote_path}"
    cmd = _base_scp(target) + [src, str(local_path)]
    proc = _run_subprocess_with_retries(cmd, timeout=600, target=target)
    if proc.returncode != 0:
        raise RuntimeError(
            _format_ssh_failure(
                target,
                detail=f"SCP download failed: {(proc.stderr or proc.stdout)[:1200]}",
            )
        )


def remote_job_dir(job_id: str) -> str:
    job_id = _validate_job_id(job_id)
    root = os.environ.get("RUNPOD_REMOTE_JOBS_ROOT", "/workspace/vsumm-jobs").strip()
    if not root.startswith("/") or ".." in root:
        root = "/workspace/vsumm-jobs"
    return f"{root}/{job_id}"


def remote_app_root() -> str:
    root = os.environ.get("RUNPOD_REMOTE_APP_ROOT", "/workspace/video-summarizer").strip()
    if not root.startswith("/") or ".." in root:
        root = "/workspace/video-summarizer"
    return root


def ensure_remote_dir(target: RunpodSshTarget, remote_dir: str) -> None:
    if ".." in remote_dir or not remote_dir.startswith("/"):
        raise ValueError("Invalid remote directory")
    ssh_run(target, f"mkdir -p {remote_dir}")


def sync_project_bundle(target: RunpodSshTarget, project_dir: Path) -> None:
    """Upload minimal app tree once per remote app root (idempotent)."""
    app_root = remote_app_root()
    marker = f"{app_root}/.vsumm_sync_ok"
    probe_timeout = _ssh_probe_timeout_sec()
    check = _run_subprocess_with_retries(
        _base_ssh(target) + [f"test -f {marker}"],
        timeout=probe_timeout,
        target=target,
    )
    if check.returncode == 0:
        return

    ssh_run(target, f"mkdir -p {app_root}", timeout=probe_timeout)
    includes: Sequence[str] = [
        "config.py",
        "scripts/runpod_heavy_worker.py",
        "scripts/runpod_nllb_batch.py",
        "src",
    ]
    tar_cmd = ["tar", "-czf", "-", *includes]
    extract = f"tar -xzf - -C {app_root} && touch {marker}"
    proc = subprocess.run(
        tar_cmd,
        cwd=str(project_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Local tar failed: {proc.stderr.decode()[:500]}")

    upload_timeout = max(600, probe_timeout * 2)
    last_err = ""
    for attempt in range(1, _ssh_retry_count() + 1):
        ssh = subprocess.Popen(
            _base_ssh(target) + [extract],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert ssh.stdin is not None
        ssh.stdin.write(proc.stdout)
        ssh.stdin.close()
        err = ssh.stderr.read().decode() if ssh.stderr else ""
        try:
            code = ssh.wait(timeout=upload_timeout)
        except subprocess.TimeoutExpired:
            ssh.kill()
            last_err = f"Remote tar upload timed out after {upload_timeout}s"
            if attempt >= _ssh_retry_count():
                raise RuntimeError(
                    _format_ssh_failure(target, detail=last_err, timed_out=True)
                ) from None
            time.sleep(_ssh_retry_delay_sec())
            continue
        if code == 0:
            return
        last_err = err[:1500] or f"Remote extract exit {code}"
        if attempt >= _ssh_retry_count():
            raise RuntimeError(_format_ssh_failure(target, detail=last_err))
        time.sleep(_ssh_retry_delay_sec())
