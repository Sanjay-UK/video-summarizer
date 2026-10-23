"""Ollama JSON translation (legacy / third backend)."""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

from config import HIGHLIGHT_TRANSLATION_MODEL, OLLAMA_HOST
from src.highlight_llm import highlight_ollama_options
from src.locales import LocaleSpec
from src.term_protection import protected_terms_hint

try:
    import ollama
except ImportError:
    ollama = None  # type: ignore


def _extract_json_object(raw: str) -> dict:
    raw = raw.strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        snippet = match.group(0)
        try:
            data = json.loads(snippet)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            data = ast.literal_eval(snippet)
            if isinstance(data, dict):
                return data
    raise ValueError("No JSON object in model response")


def _coerce_translated_field(data: dict[str, Any]) -> str:
    for key in ("translated", "translatedText", "translation", "text"):
        if key not in data or data[key] in (None, ""):
            continue
        val = data[key]
        if isinstance(val, dict):
            return _coerce_translated_field(val)
        return str(val).strip()
    raise ValueError("no translation field in JSON")


def _build_prompt(english_text: str, locale: LocaleSpec) -> str:
    terms = protected_terms_hint(english_text)
    terms_line = (
        f"Keep these terms phonetically in {locale.name} script (do not paraphrase): {terms}\n"
        if terms
        else ""
    )
    return f"""Translate the spoken narration below into {locale.name} ({locale.code}).
Rules:
- Output ONLY valid JSON: {{"translated": "..."}}
- Natural spoken {locale.name}, same meaning as English, no extra facts.
- Use native script only (no Latin letters in the translation).
- Do not replace proper nouns with invented words; transliterate them.
{terms_line}
ENGLISH:
{english_text.strip()}
"""


class OllamaSceneTranslator:
    def __init__(self, model: Optional[str] = None):
        self.model = model or HIGHLIGHT_TRANSLATION_MODEL

    def translate(self, english_text: str, locale: LocaleSpec, *, scene_index: int) -> str:
        _ = scene_index
        if ollama is None:
            raise RuntimeError("ollama package not installed")
        client = ollama.Client(host=OLLAMA_HOST)
        response = client.generate(
            model=self.model,
            prompt=_build_prompt(english_text, locale),
            stream=False,
            options=highlight_ollama_options(num_ctx=4096),
        )
        raw = response.get("response", "").strip()
        try:
            data = _extract_json_object(raw)
            translated = _coerce_translated_field(data)
        except ValueError as exc:
            if "{" in raw and len(raw) > 200:
                raise ValueError("Ollama returned non-JSON or partial JSON for long text") from exc
            translated = raw
        if not translated:
            raise ValueError("empty translated field")
        if "{" in translated and '"translated"' in translated.lower():
            raise ValueError("Ollama leaked JSON wrapper into translation text")
        return translated
