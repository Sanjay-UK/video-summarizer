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
from src.highlight_jobs import highlight_file_for_lang  # noqa: E402
from src.highlight_pipeline import run_highlight_pipeline  # noqa: E402
from src.locales import parse_language_list  # noqa: E402
from src.runpod_serverless_payload import (  # noqa: E402
    decode_audio_to_path,
    decode_video_to_path,
    encode_file_gzip_b64,
)
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
        # What was skipped or fell back, and how reliable the transcript is. Shown on the results page.
        for notes_name, out_key in (("summary_metadata.json", "summary_metadata_json"),
                                    ("transcript_notes.json", "transcript_notes_json")):
            notes_path = workdir / notes_name
            if notes_path.is_file():
                out[out_key] = notes_path.read_text(encoding="utf-8")
        # Subtitles and the summary timeline need this file; the summarizer writes it on the worker.
        seg_path = workdir / "summary_segments.json"
        if seg_path.is_file():
            out["summary_segments_json"] = seg_path.read_text(encoding="utf-8")
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
    import torch  # noqa: F401 — base-image CUDA torch before transformers NLLB load

    from src.translators.nllb_backend import _NllbEngine

    _NllbEngine.get()
    return {
        "ok": True,
        "phase": "warmup",
        "whisper_model": whisper,
        "summarization_model": ollama,
        "nllb_model": inp.get("nllb_model"),
    }


def _diagnostic() -> dict[str, Any]:
    """Report library versions and torch detection on this worker. No model loads, no secrets."""
    import importlib.metadata as md

    info: dict[str, Any] = {"ok": True, "phase": "diagnostic", "python": sys.executable, "sys_path": sys.path[:12]}
    for pkg in ("torch", "transformers", "tokenizers", "huggingface-hub"):
        try:
            info[pkg] = md.version(pkg)
        except Exception as exc:  # noqa: BLE001
            info[pkg] = f"ERROR {type(exc).__name__}: {exc}"
    try:
        import transformers.utils.import_utils as iu

        info["transformers_is_torch_available"] = iu.is_torch_available()
    except Exception as exc:  # noqa: BLE001
        info["transformers_is_torch_available"] = f"ERROR {type(exc).__name__}: {exc}"
    try:
        import torch

        info["torch_version"] = torch.__version__
        info["torch_file"] = torch.__file__
        info["cuda_available"] = torch.cuda.is_available()
    except Exception as exc:  # noqa: BLE001
        info["torch_import"] = f"ERROR {type(exc).__name__}: {exc}"
    info["env_names"] = sorted(
        k for k in os.environ if any(s in k.upper() for s in ("TRANSFORMERS", "TORCH", "HF_", "USE_", "PYTHON", "CUDA"))
    )
    return info


def _highlight_phase(inp: dict[str, Any]) -> dict[str, Any]:
    model = inp.get("summarization_model", "llama3.2:3b")
    _warm_ollama(model)
    languages = inp.get("languages") or "en"
    compression = inp.get("compression", "medium")
    segments_raw = inp.get("segments_json")
    if not segments_raw:
        raise ValueError("Missing segments_json in highlight_phase input")
    if isinstance(segments_raw, str):
        segments = json.loads(segments_raw)
    elif isinstance(segments_raw, list):
        segments = segments_raw
    else:
        raise ValueError("segments_json must be a JSON string or list")
    with tempfile.TemporaryDirectory(prefix="vsumm_highlight_") as tmp:
        workdir = Path(tmp)
        video_name = inp.get("video_file") or "source.mp4"
        decode_video_to_path(inp, workdir / video_name)
        staged = workdir / video_name
        run_highlight_pipeline(
            staged,
            segments,
            workdir,
            model=model,
            compression=compression,
            languages=languages,
        )
        status_path = workdir / "highlights" / "highlight_status.json"
        out: dict[str, Any] = {
            "highlight_status_json": status_path.read_text(encoding="utf-8")
            if status_path.is_file()
            else "{}",
            "highlights_gzip_b64": {},
        }
        for lang in parse_language_list(languages):
            mp4 = highlight_file_for_lang(workdir, lang)
            if mp4.is_file():
                out["highlights_gzip_b64"][lang] = encode_file_gzip_b64(mp4)
        return out


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
        if job_type == "diagnostic":
            return _diagnostic()
        if job_type == "highlight_phase":
            return _highlight_phase(inp)
        raise ValueError(f"Unknown job_type: {job_type}")


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
