"""Build and push the RunPod serverless worker image (Docker Desktop + Hub)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"
DOCKERFILE = PROJECT_DIR / "runpod_worker" / "Dockerfile"
LOCAL_TAG = "video-summarizer-worker:v1.0.12"
DEFAULT_IMAGE_NAME = "video-summarizer-worker"
DEFAULT_IMAGE_TAG = "v1.0.12"

LogFn = Callable[[str], None]


def _log_default(msg: str) -> None:
    pass


def merge_env_file(updates: dict[str, str]) -> list[str]:
    """Update .env and apply the same keys to os.environ."""
    lines: list[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    index: dict[str, int] = {}
    for i, raw in enumerate(lines):
        if "=" in raw and not raw.strip().startswith("#"):
            index[raw.split("=", 1)[0].strip()] = i
    changed: list[str] = []
    for key, value in updates.items():
        entry = f"{key}={value}"
        if key in index:
            if lines[index[key]] != entry:
                lines[index[key]] = entry
                changed.append(key)
        else:
            lines.append(entry)
            changed.append(key)
        os.environ[key] = value
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def normalize_docker_hub_user(raw: str | None) -> tuple[str | None, str | None]:
    """Return (username, error_message). Empty input is allowed (no update)."""
    if raw is None:
        return None, None
    user = raw.strip()
    if not user:
        return None, None
    if "/" in user or " " in user or "@" in user:
        return None, "Docker Hub username must not contain /, spaces, or @."
    if len(user) > 30:
        return None, "Docker Hub username is too long (max 30 characters)."
    return user, None


def apply_docker_hub_user(raw: str | None) -> dict[str, Any]:
    """Persist DOCKER_HUB_USER to .env and os.environ when non-empty."""
    user, err = normalize_docker_hub_user(raw)
    if err:
        return {"ok": False, "error": err}
    if not user:
        return {"ok": True, "skipped": True, "user": None}
    merge_env_file({"DOCKER_HUB_USER": user})
    os.environ["DOCKER_HUB_USER"] = user
    return {"ok": True, "user": user}


def _docker_hub_user() -> str | None:
    for key in (
        "DOCKER_HUB_USER",
        "DOCKERHUB_USER",
        "DOCKER_REGISTRY_USER",
        "DOCKER_USER",
    ):
        val = os.environ.get(key, "").strip()
        if val and "/" not in val:
            return val
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            m = re.match(r"\s*Username:\s*(\S+)", line)
            if m:
                return m.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _docker_cli_installed() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Client.Version}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return proc.returncode == 0
    except OSError:
        return False


def start_docker_desktop() -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Docker"], check=False)


def wait_for_docker_daemon(*, timeout_sec: int = 120, log: LogFn = _log_default) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if proc.returncode == 0:
                log("Docker daemon is ready.")
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        log("Waiting for Docker daemon…")
        time.sleep(3)
    return False


def remote_image_ref() -> tuple[str | None, str | None]:
    """
    Return (full_image_ref, error_message).
    Uses DOCKER_HUB_USER / docker info Username.
    """
    user = _docker_hub_user()
    if not user:
        return None, (
            "No Docker Hub user found. Set DOCKER_HUB_USER in .env or run "
            "`docker login` so `docker info` shows Username."
        )
    name = os.environ.get("RUNPOD_SERVERLESS_IMAGE_NAME", DEFAULT_IMAGE_NAME).strip()
    tag = os.environ.get("RUNPOD_SERVERLESS_IMAGE_TAG", DEFAULT_IMAGE_TAG).strip()
    return f"{user}/{name}:{tag}", None


def apply_registry_image(image: str) -> dict[str, Any]:
    """Use a pre-built image (Hub deploy, Docker Hub, etc.) — no local Docker."""
    from src.runpod_serverless import serverless_image_registry_ready

    ref = (image or "").strip()
    if not serverless_image_registry_ready(ref):
        return {
            "ok": False,
            "error": (
                "Registry image must look like docker.io/user/repo:tag (include a /). "
                "The hub one-click deploy creates an endpoint in your cloud console; "
                "to use an image with this app, copy the container image from that endpoint "
                "or build from runpod_worker/Dockerfile via Hub/GitHub."
            ),
        }
    merge_env_file({"RUNPOD_SERVERLESS_IMAGE": ref})
    return {"ok": True, "image": ref, "steps": [f"Using registry image {ref}"]}


def sync_serverless_env_from_selection() -> list[str]:
    """Align .env with saved GPU selection (DC, pools, volume reuse)."""
    sel_path = PROJECT_DIR / "data" / "compute_selection.json"
    if not sel_path.is_file():
        return []
    import json

    saved = json.loads(sel_path.read_text(encoding="utf-8"))
    offer = saved.get("offer") if isinstance(saved.get("offer"), dict) else {}
    dc = (
        str(saved.get("serverless_data_center_id") or "").strip()
        or str(offer.get("data_center_id") or "").strip()
    )
    pool = str(offer.get("serverless_pool") or "").strip()
    deploy = saved.get("deploy") if isinstance(saved.get("deploy"), dict) else {}
    updates: dict[str, str] = {"RUNPOD_COMPUTE_MODE": "serverless"}
    if dc:
        updates["RUNPOD_DATA_CENTER_ID"] = dc
    if pool:
        updates["RUNPOD_SERVERLESS_GPU_POOLS"] = pool
    vol = str(deploy.get("network_volume_id") or "").strip()
    if vol:
        updates["RUNPOD_NETWORK_VOLUME_ID"] = vol
    return merge_env_file(updates)


def ensure_serverless_worker_image(
    *,
    log: LogFn = _log_default,
    docker_wait_sec: int = 120,
    build_timeout_sec: int = 3600,
) -> dict[str, Any]:
    """
    Start Docker, build linux/amd64 worker image, push to Docker Hub, update RUNPOD_SERVERLESS_IMAGE.
    """
    steps: list[str] = []

    def step(msg: str) -> None:
        steps.append(msg)
        log(msg)

    if not _docker_cli_installed():
        return {
            "ok": False,
            "error": "Docker CLI not found. Install Docker Desktop and ensure `docker` is on PATH.",
            "steps": steps,
        }

    if not DOCKERFILE.is_file():
        return {
            "ok": False,
            "error": f"Missing Dockerfile at {DOCKERFILE}",
            "steps": steps,
        }

    step("Starting Docker Desktop (if needed)…")
    start_docker_desktop()
    if not wait_for_docker_daemon(timeout_sec=docker_wait_sec, log=step):
        return {
            "ok": False,
            "error": (
                f"Docker daemon did not become ready within {docker_wait_sec}s. "
                "Ensure Docker Desktop is installed and can start."
            ),
            "steps": steps,
        }

    remote, user_err = remote_image_ref()
    if user_err or not remote:
        return {"ok": False, "error": user_err or "Could not determine remote image name.", "steps": steps}

    step(f"Building {LOCAL_TAG} (platform linux/amd64)…")
    try:
        build = subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "-f",
                str(DOCKERFILE),
                "-t",
                LOCAL_TAG,
                str(PROJECT_DIR),
            ],
            capture_output=True,
            text=True,
            timeout=build_timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Docker build timed out after {build_timeout_sec}s.",
            "steps": steps,
        }
    if build.returncode != 0:
        tail = (build.stderr or build.stdout or "")[-3000:]
        return {
            "ok": False,
            "error": f"Docker build failed (exit {build.returncode}).",
            "build_log_tail": tail,
            "steps": steps,
        }

    id_proc = subprocess.run(
        ["docker", "images", "-q", LOCAL_TAG],
        capture_output=True,
        text=True,
        check=False,
    )
    image_id = (id_proc.stdout or "").strip().split("\n")[0] or None
    step(f"Build succeeded (image id {image_id or 'unknown'}).")

    step(f"Tagging and pushing {remote}…")
    subprocess.run(["docker", "tag", LOCAL_TAG, remote], check=False, timeout=60)
    try:
        push = subprocess.run(
            ["docker", "push", remote],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "Docker push timed out after 1800s.",
            "steps": steps,
            "image": remote,
        }
    if push.returncode != 0:
        err = (push.stderr or push.stdout or "").strip()
        hint = ""
        if "denied" in err.lower() or "unauthorized" in err.lower():
            hint = " Run `docker login` or set DOCKER_HUB_USER with valid Hub credentials."
        return {
            "ok": False,
            "error": f"Docker push failed: {err[:800]}{hint}",
            "steps": steps,
            "image": remote,
        }

    merge_env_file({"RUNPOD_SERVERLESS_IMAGE": remote})
    step(f"Updated .env RUNPOD_SERVERLESS_IMAGE={remote}")

    return {
        "ok": True,
        "image": remote,
        "image_id": image_id,
        "steps": steps,
    }
