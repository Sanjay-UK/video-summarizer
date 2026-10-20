"""Offline NLLB-200 distilled 600M (transformers — optional heavy dependency)."""

from __future__ import annotations

import sys
from typing import Optional

from src.locales import LocaleSpec

# FLORES-200 codes for facebook/nllb-200-distilled-600M
_NLLB_LANG = {
    "en": "eng_Latn",
    "hi": "hin_Deva",
    "kn": "kan_Knda",
    "te": "tel_Telu",
    "ta": "tam_Tamil",
}

_MODEL_ID = "facebook/nllb-200-distilled-600M"


class NllbNotAvailableError(RuntimeError):
    pass


class _NllbEngine:
    _instance: Optional["_NllbEngine"] = None

    def __init__(self) -> None:
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise NllbNotAvailableError(
                "NLLB backend requires: pip install torch transformers sentencepiece accelerate"
            ) from exc

        print(f"[INFO] Loading NLLB model {_MODEL_ID} (first run downloads ~2.4GB)...", file=sys.stderr)
        self.tokenizer = AutoTokenizer.from_pretrained(_MODEL_ID)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(_MODEL_ID)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)

    @classmethod
    def get(cls) -> "_NllbEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


class NllbSceneTranslator:
    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
        _ = scene_index
        if locale.code == "en":
            return english_text.strip()

        engine = _NllbEngine.get()
        import torch

        src = _NLLB_LANG["en"]
        tgt = _NLLB_LANG[locale.code]
        engine.tokenizer.src_lang = src
        inputs = engine.tokenizer(english_text.strip(), return_tensors="pt").to(engine.device)
        forced_bos = engine.tokenizer.convert_tokens_to_ids(tgt)
        with torch.no_grad():
            tokens = engine.model.generate(
                **inputs,
                forced_bos_token_id=forced_bos,
                max_length=256,
            )
        out = engine.tokenizer.decode(tokens[0], skip_special_tokens=True).strip()
        if not out:
            raise RuntimeError("NLLB returned empty translation")
        return out


def nllb_backend_available() -> bool:
    try:
        import torch  # noqa: F401
        from transformers import AutoModelForSeq2SeqLM  # noqa: F401
    except ImportError:
        return False
    return True
