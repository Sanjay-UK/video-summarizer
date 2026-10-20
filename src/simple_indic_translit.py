"""Minimal offline Latin→Indic transliteration when indic-transliteration is unavailable."""

from __future__ import annotations

import re

_GLOSSARY: dict[str, dict[str, str]] = {
    "sushi": {"hi": "सुशी", "kn": "ಸುಷಿ", "te": "సుషి", "ta": "சுஷி"},
    "nagiri": {"hi": "नगिरी", "kn": "ನಗಿರಿ", "te": "నగిరి", "ta": "நகிரி"},
    "nigiri": {"hi": "निगिरी", "kn": "ನಿಗಿರಿ", "te": "నిగిరి", "ta": "நிகிரி"},
    "maki": {"hi": "माकी", "kn": "ಮಾಕಿ", "te": "మాకి", "ta": "மாகி"},
    "handroll": {"hi": "हैंडरोल", "kn": "ಹ್ಯಾಂಡ್‌ರೋಲ್", "te": "హ్యాండ్‌రోల్", "ta": "ஹாண்ட்ரோல்"},
    "rice": {"hi": "राइस", "kn": "ರೈಸ್", "te": "రైస్", "ta": "ரைஸ்"},
    "kombu": {"hi": "कोम्बू", "kn": "ಕೊಂಬು", "te": "కోంబు", "ta": "கோம்பு"},
    "umami": {"hi": "उमामी", "kn": "ಉಮಾಮಿ", "te": "ఉమామి", "ta": "உமாமி"},
    "python": {"hi": "पायथन", "kn": "ಪೈಥಾನ್", "te": "పైథాన్", "ta": "பைத்தான்"},
    "cpu": {"hi": "सीपीयू", "kn": "ಸಿಪಿಯು", "te": "సిపియు", "ta": "சிபியு"},
    "api": {"hi": "एपीआई", "kn": "ಎಪಿಐ", "te": "ఏపీఐ", "ta": "ஏபிஐ"},
}

_LATIN_ONLY = re.compile(r"^[A-Za-z0-9+\-.'’]+$")


def glossary_transliterate(term: str, lang_code: str) -> str | None:
    key = term.lower().replace("-", "")
    entry = _GLOSSARY.get(key)
    if entry and lang_code in entry:
        return entry[lang_code]
    return None


def try_transliterate(term: str, lang_code: str) -> tuple[str, bool]:
    """
    Returns (form, success). On failure, caller should keep the original term.
    Never returns empty or a degraded lowercase-only stub.
    """
    original = term
    hit = glossary_transliterate(term, lang_code)
    if hit:
        return hit, True

    phonetic = term.replace("-", "")
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate

        script = {
            "hi": sanscript.DEVANAGARI,
            "kn": sanscript.KANNADA,
            "te": sanscript.TELUGU,
            "ta": sanscript.TAMIL,
        }[lang_code]
        out = transliterate(phonetic.lower(), sanscript.ITRANS, script)
        if not out or out.strip() == original.strip():
            return original, False
        if _LATIN_ONLY.match(out):
            return original, False
        return out, True
    except ImportError:
        return original, False
    except Exception:
        return original, False


def simple_transliterate(term: str, lang_code: str) -> str:
    """Backward-compatible: returns original when transliteration fails."""
    out, ok = try_transliterate(term, lang_code)
    return out if ok else term
