"""Route NLLB inference to RunPod when hybrid GPU mode is active."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from src.locales import LocaleSpec
from src.runpod_ssh import (
    copy_from_remote,
    copy_to_remote,
    ensure_remote_dir,
    get_runpod_ssh_target,
    remote_app_root,
    ssh_run,
    sync_project_bundle,
)

_PROJECT_DIR = Path(__file__).resolve().parent.parent


def nllb_via_runpod_enabled() -> bool:
    return os.environ.get("NLLB_VIA_RUNPOD", "").strip() == "1"


def translate_texts_via_runpod(language_code: str, english_texts: list[str]) -> list[str]:
    if not english_texts:
        return []
    target = get_runpod_ssh_target()
    sync_project_bundle(target, _PROJECT_DIR)
    batch_id = uuid.uuid4().hex[:12]
    remote_batch_dir = f"{remote_app_root()}/nllb-batches/{batch_id}"
    ensure_remote_dir(target, remote_batch_dir)

    with tempfile.TemporaryDirectory(prefix="vsumm_nllb_") as tmp:
        tmp_path = Path(tmp)
        in_file = tmp_path / "in.json"
        out_file = tmp_path / "out.json"
        in_file.write_text(
            json.dumps({"language": language_code, "texts": english_texts}, ensure_ascii=False),
            encoding="utf-8",
        )
        remote_in = f"{remote_batch_dir}/in.json"
        remote_out = f"{remote_batch_dir}/out.json"
        copy_to_remote(target, in_file, remote_in)
        app_root = remote_app_root()
        remote_cmd = (
            f"cd {app_root} && python3 scripts/runpod_nllb_batch.py "
            f"--input {remote_in} --output {remote_out}"
        )
        ssh_run(target, remote_cmd, timeout=3600)
        copy_from_remote(target, remote_out, out_file)
        payload = json.loads(out_file.read_text(encoding="utf-8"))
        texts = payload.get("texts") or []
        if len(texts) != len(english_texts):
            raise RuntimeError("RunPod NLLB batch returned unexpected text count")
        return [str(t) for t in texts]


def translate_scene_via_runpod(english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
    _ = scene_index
    results = translate_texts_via_runpod(locale.code, [english_text])
    return results[0]
