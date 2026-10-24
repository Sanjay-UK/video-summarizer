"""RunPod Serverless worker: Whisper, Ollama summarize, NLLB."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# App root = /app in Docker
APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

import runpod  # noqa: E402
from runpod.serverless import VolumeCache  # noqa: E402

from src.locales import get_locale  # noqa: E402
from src.runpod_heavy_core import run_heavy_phase  # noqa: E402
from src.runpod_serverless_payload import decode_audio_to_path  # noqa: E402
from src.translators.nllb_backend import NllbSceneTranslator  # noqa: E402

CACHE_DIRS = [
    "/root/.cache/huggingface",
    "/root/.ollama",
]

_ollama_warmed: set[str] = set()


def _warm_ollama(model: str) -> None:
    if model in _ollama_warmed:
        return
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    try:
        import urllib.request

        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps({"model": model, "keep_alive": -1}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=600)
    except Exception:
        subprocess.run(["ollama", "pull", model], check=False, timeout=3600)
        _ollama_warmed.add(model)
        return
    _ollama_warmed.add(model)


def _heavy_phase(inp: dict[str, Any]) -> dict[str, Any]:
    model = inp.get("summarization_model", "llama3.2:3b")
    _warm_ollama(model)
    with tempfile.TemporaryDirectory(prefix="vsumm_heavy_") as tmp:
        workdir = Path(tmp)
        decode_audio_to_path(inp, workdir / (inp.get("audio_file") or "audio.mp3"))
        manifest = {
            "audio_file": inp.get("audio_file") or "audio.mp3",
            "whisper_model": inp.get("whisper_model"),
            "summarization_model": model,
            "compression": inp.get("compression", "medium"),
            "highlight_languages": inp.get("highlight_languages", "en"),
        }
        (workdir / "heavy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        run_heavy_phase(workdir, manifest)
        segments_raw = (workdir / "segments.json").read_text(encoding="utf-8")
        st_path = workdir / "summary_translations.json"
        out: dict[str, Any] = {
            "transcript": (workdir / "transcript.txt").read_text(encoding="utf-8"),
            "segments_json": segments_raw,
            "summary": (workdir / "summary.txt").read_text(encoding="utf-8"),
            "heavy_done_json": (workdir / "heavy_done.json").read_text(encoding="utf-8"),
        }
        if st_path.is_file():
            out["summary_translations_json"] = st_path.read_text(encoding="utf-8")
        return out


def _load_whisper(model_name: str) -> str:
    from faster_whisper import WhisperModel

    os.environ["WHISPER_DEVICE"] = "cuda"
    WhisperModel(model_name, device="cuda", compute_type="float16")
    return model_name


def _warmup(inp: dict[str, Any]) -> dict[str, Any]:
    whisper = inp.get("whisper_model") or "base"
    ollama = inp.get("summarization_model") or inp.get("ollama_model") or "llama3.2:3b"
    _warm_ollama(ollama)
    _load_whisper(whisper)
    from src.translators.nllb_backend import _NllbEngine

    _NllbEngine.get()
    return {
        "ok": True,
        "phase": "warmup",
        "whisper_model": whisper,
        "summarization_model": ollama,
        "nllb_model": inp.get("nllb_model"),
    }


def _nllb_batch(inp: dict[str, Any]) -> dict[str, Any]:
    lang = inp["language"]
    texts = inp["texts"]
    locale = get_locale(lang)
    translator = NllbSceneTranslator()
    out_texts = []
    for idx, text in enumerate(texts):
        out_texts.append(translator.translate(text, locale, scene_index=idx))
    return {"language": lang, "texts": out_texts}


def handler(job: dict[str, Any]) -> dict[str, Any]:
    inp = job.get("input") or {}
    job_type = inp.get("job_type", "heavy_phase")
    with VolumeCache(dirs=CACHE_DIRS):
        if job_type == "nllb_batch":
            return _nllb_batch(inp)
        if job_type == "heavy_phase":
            return _heavy_phase(inp)
        if job_type == "warmup":
            return _warmup(inp)
        raise ValueError(f"Unknown job_type: {job_type}")


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
