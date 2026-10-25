#!/usr/bin/env python3
"""Validate a dependency environment before switching the app to it.

Run with the interpreter of the environment under test, e.g.:
  venv-mac/bin/python scripts/validate_env.py

Checks (each reported separately, exit 1 if any fails):
  1. Python and platform
  2. torch install manifest (RECORD) matches files on disk
  3. torch import, expected version, CPU tensor operation
  4. transformers loads a seq2seq checkpoint from local disk (exercises the torch.load gate)
  5. Whisper base loads on CPU and transcribes silence (cached model only)
  6. NLLB-200 translates one sentence (cached model only)
  7. Ollama model present

Offline mode is forced: nothing is downloaded. Missing caches are reported as SKIPPED, not failed.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_PYTHON = (3, 11)
EXPECTED_TORCH = "2.4.1"
EXPECTED_TRANSFORMERS = "4.49.0"
NLLB_MODEL = "facebook/nllb-200-distilled-600M"
OLLAMA_MODEL = "llama3.2:3b"

results: list[dict] = []


def record(name: str, status: str, detail: str = "", seconds: float | None = None) -> None:
    results.append({"check": name, "status": status, "detail": detail, "seconds": seconds})
    print(f"[{status}] {name}: {detail}", flush=True)


def run(name: str, fn) -> None:
    t0 = time.monotonic()
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001
        status, detail = "FAIL", f"{type(exc).__name__}: {str(exc)[:400]}"
    record(name, status, detail, round(time.monotonic() - t0, 2))


def check_python() -> tuple[str, str]:
    v = sys.version_info[:2]
    plat = f"{platform.system()} {platform.machine()}"
    ok = v == EXPECTED_PYTHON and platform.system() == "Darwin" and platform.machine() == "arm64"
    return ("PASS" if ok else "FAIL", f"python {platform.python_version()} on {plat}")


def check_record() -> tuple[str, str]:
    """Every file listed in the installed packages' RECORD must exist. Catches partial deletions."""
    import importlib.metadata as md

    missing: list[str] = []
    checked = 0
    for dist in md.distributions():
        if not dist.files:
            continue
        for f in dist.files:
            if str(f).endswith((".pyc", "RECORD")):
                continue
            checked += 1
            if not Path(dist.locate_file(f)).exists():
                missing.append(f"{dist.metadata['Name']}: {f}")
    if missing:
        return "FAIL", f"{len(missing)} missing of {checked} recorded files, e.g. {missing[:3]}"
    return "PASS", f"{checked} recorded files present"


def check_torch() -> tuple[str, str]:
    import torch

    if torch.__version__.split("+")[0] != EXPECTED_TORCH:
        return "FAIL", f"torch {torch.__version__}, expected {EXPECTED_TORCH}"
    a = torch.arange(6, dtype=torch.float32).reshape(2, 3)
    b = torch.ones(3, 2)
    out = (a @ b).sum().item()
    if out != 30.0:
        return "FAIL", f"CPU matmul returned {out}, expected 30.0"
    return "PASS", f"torch {torch.__version__}, CPU matmul ok, device={a.device}"


def check_transformers_load() -> tuple[str, str]:
    import torch  # noqa: F401
    import transformers
    from transformers import AutoModelForSeq2SeqLM, T5Config, T5ForConditionalGeneration

    if transformers.__version__ != EXPECTED_TRANSFORMERS:
        return "FAIL", f"transformers {transformers.__version__}, expected {EXPECTED_TRANSFORMERS}"
    cfg = T5Config(vocab_size=64, d_model=16, d_ff=32, num_layers=1, num_heads=2, d_kv=8)
    with tempfile.TemporaryDirectory(prefix="validate_t5_") as tmp:
        T5ForConditionalGeneration(cfg).save_pretrained(tmp, safe_serialization=False)  # .bin, like NLLB
        model = AutoModelForSeq2SeqLM.from_pretrained(tmp)
    return "PASS", f"transformers {transformers.__version__} loaded a .bin seq2seq checkpoint ({type(model).__name__})"


def check_whisper() -> tuple[str, str]:
    cache = Path.home() / ".cache/huggingface/hub/models--Systran--faster-whisper-base"
    if not cache.exists():
        return "SKIPPED", "faster-whisper-base not in local cache"
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
    segments, info = model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)
    list(segments)
    return "PASS", f"faster-whisper base loaded on CPU (int8), language={info.language}"


def check_nllb() -> tuple[str, str]:
    cache = Path.home() / ".cache/huggingface/hub/models--facebook--nllb-200-distilled-600M"
    if not cache.exists():
        return "SKIPPED", f"{NLLB_MODEL} not in local cache"
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(NLLB_MODEL, src_lang="eng_Latn", local_files_only=True)
    model = AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL, local_files_only=True)
    inputs = tok("Good morning.", return_tensors="pt")
    out = model.generate(**inputs, forced_bos_token_id=tok.convert_tokens_to_ids("hin_Deva"), max_new_tokens=32)
    text = tok.batch_decode(out, skip_special_tokens=True)[0]
    if not text.strip():
        return "FAIL", "NLLB generated empty output"
    return "PASS", f"NLLB eng->hin translation: {text!r}"


def check_ollama() -> tuple[str, str]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            tags = json.load(r)
    except Exception as exc:  # noqa: BLE001
        return "SKIPPED", f"Ollama not reachable: {type(exc).__name__}"
    names = {m.get("name") for m in tags.get("models", [])}
    if OLLAMA_MODEL in names:
        return "PASS", f"{OLLAMA_MODEL} present"
    return "FAIL", f"{OLLAMA_MODEL} not in Ollama (have {sorted(names)[:5]})"


def main() -> int:
    print(f"validating {sys.executable}", flush=True)
    run("python_platform", check_python)
    run("torch_install_manifest", check_record)
    run("torch_import_and_cpu_ops", check_torch)
    run("transformers_seq2seq_load", check_transformers_load)
    run("whisper_base_cpu", check_whisper)
    run("nllb_translation", check_nllb)
    run("ollama_model", check_ollama)
    failed = [r["check"] for r in results if r["status"] == "FAIL"]
    skipped = [r["check"] for r in results if r["status"] == "SKIPPED"]
    summary = {"python": sys.executable, "passed": len(results) - len(failed) - len(skipped),
               "failed": failed, "skipped": skipped, "results": results}
    print(json.dumps(summary, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
