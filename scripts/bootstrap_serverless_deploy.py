#!/usr/bin/env python3
"""Sync serverless .env from saved GPU selection, wait for Docker, build worker image."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"
SELECTION_PATH = PROJECT_DIR / "data" / "compute_selection.json"
IMAGE_LOCAL = "video-summarizer-worker:v1.0.0"
DOCKERFILE = PROJECT_DIR / "runpod_worker" / "Dockerfile"

PLACEHOLDER_IMAGE_PREFIXES = ("your-registry/", "YOUR_REGISTRY/", "YOUR_DOCKERHUB_USER/")


def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return out
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _write_env(updates: dict[str, str]) -> list[str]:
    """Merge keys into .env; returns list of keys changed."""
    lines: list[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    index: dict[str, int] = {}
    for i, raw in enumerate(lines):
        if "=" in raw and not raw.strip().startswith("#"):
            key = raw.split("=", 1)[0].strip()
            index[key] = i
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
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def _load_selection() -> dict:
    if not SELECTION_PATH.is_file():
        return {}
    return json.loads(SELECTION_PATH.read_text(encoding="utf-8"))


def _patch_selection_deploy(dc: str, pools: list[str]) -> None:
    saved = _load_selection()
    if not saved:
        return
    deploy = dict(saved.get("deploy") or {})
    if deploy.get("network_volume_id") and not deploy.get("data_center_id"):
        deploy["data_center_id"] = dc
        deploy["serverless_gpu_pools"] = pools
        saved["deploy"] = deploy
        SELECTION_PATH.write_text(json.dumps(saved, indent=2), encoding="utf-8")


def _sync_env_from_selection() -> dict[str, str]:
    saved = _load_selection()
    offer = saved.get("offer") if isinstance(saved.get("offer"), dict) else {}
    dc = (
        str(saved.get("serverless_data_center_id") or "").strip()
        or str(offer.get("data_center_id") or "").strip()
        or "CA-MTL-1"
    )
    pool = str(offer.get("serverless_pool") or "AMPERE_24").strip()
    pools = pool or "AMPERE_24"
    deploy = saved.get("deploy") if isinstance(saved.get("deploy"), dict) else {}
    volume_id = str(deploy.get("network_volume_id") or "").strip()

    updates: dict[str, str] = {
        "RUNPOD_COMPUTE_MODE": "serverless",
        "RUNPOD_DATA_CENTER_ID": dc,
        "RUNPOD_SERVERLESS_GPU_POOLS": pools,
    }
    if volume_id:
        updates["RUNPOD_NETWORK_VOLUME_ID"] = volume_id

    _patch_selection_deploy(dc, [p.strip() for p in pools.split(",") if p.strip()])
    return updates


def _docker_username() -> str | None:
    for key in ("DOCKERHUB_USER", "DOCKER_REGISTRY_USER", "DOCKER_USER"):
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


def _wait_for_docker(timeout_sec: int = 120) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
    return False


def _start_docker_desktop() -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Docker"], check=False)


def _build_image() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            str(DOCKERFILE),
            "-t",
            IMAGE_LOCAL,
            str(PROJECT_DIR),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _image_id(local_tag: str) -> str | None:
    proc = subprocess.run(
        ["docker", "images", "-q", local_tag],
        capture_output=True,
        text=True,
        check=False,
    )
    tag = (proc.stdout or "").strip().split("\n")[0].strip()
    return tag or None


def _try_push(remote_tag: str) -> tuple[bool, str]:
    subprocess.run(["docker", "tag", IMAGE_LOCAL, remote_tag], check=False)
    proc = subprocess.run(
        ["docker", "push", remote_tag],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True, proc.stdout or "push ok"
    return False, (proc.stderr or proc.stdout or "push failed").strip()


def main() -> int:
    env_updates = _sync_env_from_selection()
    changed = _write_env(env_updates)
    print(f"[bootstrap] .env updated keys: {', '.join(changed) or '(none)'}")

    saved = _load_selection()
    models = saved.get("serverless_models") or {}
    if str(models.get("worker_image_source") or "") == "registry":
        from src.serverless_worker_image_publish import apply_registry_image

        reg = str(models.get("registry_image") or _read_env().get("RUNPOD_SERVERLESS_IMAGE", "")).strip()
        result = apply_registry_image(reg)
        if result.get("ok"):
            print(f"[bootstrap] Using registry image (no Docker): {result.get('image')}")
            return 0
        print(f"[bootstrap] FAIL: {result.get('error')}")
        return 1

    print("[bootstrap] Starting Docker Desktop (macOS)...")
    _start_docker_desktop()

    existing = _read_env()
    img = existing.get("RUNPOD_SERVERLESS_IMAGE", "")
    if not img or img.startswith(PLACEHOLDER_IMAGE_PREFIXES):
        print("[bootstrap] RUNPOD_SERVERLESS_IMAGE not set (will set after build/push if possible)")

    print("[bootstrap] Waiting for Docker daemon (up to 120s)...")
    if not _wait_for_docker(120):
        print("[bootstrap] FAIL: Docker daemon not ready")
        return 1
    print("[bootstrap] Docker daemon ready")

    print(f"[bootstrap] Building {IMAGE_LOCAL} ...")
    build = _build_image()
    if build.returncode != 0:
        print(build.stdout[-4000:] if build.stdout else "")
        print(build.stderr[-4000:] if build.stderr else "")
        print(f"[bootstrap] FAIL: docker build exit {build.returncode}")
        return build.returncode

    iid = _image_id(IMAGE_LOCAL)
    print(f"[bootstrap] Build OK image_id={iid or 'unknown'}")

    user = _docker_username()
    remote = existing.get("DOCKERHUB_USER") or user
    registry = existing.get("DOCKER_REGISTRY", "").strip().rstrip("/")
    if remote:
        remote_tag = f"{remote}/video-summarizer-worker:v1.0.0"
        if registry:
            remote_tag = f"{registry}/{remote_tag}" if "/" not in registry else f"{registry}/video-summarizer-worker:v1.0.0"
        print(f"[bootstrap] Attempting push {remote_tag} ...")
        ok, msg = _try_push(remote_tag)
        if ok:
            _write_env({"RUNPOD_SERVERLESS_IMAGE": remote_tag})
            print(f"[bootstrap] Push OK; RUNPOD_SERVERLESS_IMAGE={remote_tag}")
            return 0
        print(f"[bootstrap] Push failed: {msg[:500]}")
        _write_env({"RUNPOD_SERVERLESS_IMAGE": remote_tag})
        print(
            "[bootstrap] Set RUNPOD_SERVERLESS_IMAGE to intended remote tag; "
            "RunPod cannot pull until push succeeds (run: docker login && docker push "
            f"{remote_tag})"
        )
        return 0

    _write_env({"RUNPOD_SERVERLESS_IMAGE": IMAGE_LOCAL})
    print(
        "[bootstrap] No Docker Hub username (docker login / DOCKERHUB_USER in .env). "
        f"Local image only: {IMAGE_LOCAL}. RunPod needs a registry push."
    )
    print(
        "  Next: docker login && docker tag video-summarizer-worker:v1.0.0 "
        "YOUR_USER/video-summarizer-worker:v1.0.0 && docker push YOUR_USER/video-summarizer-worker:v1.0.0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
