"""Transcribe audio using faster-whisper with domain-specific term correction."""

import json

from src.job_progress import report_progress
import sys
from pathlib import Path
from config import OUTPUT_DIR, SUBTITLE_GEN_DIR, WHISPER_DEVICE, WHISPER_MODEL

# Whisper's average log-probability per segment. Below this the text is mostly guesses (faster-whisper's own threshold).
LOW_CONFIDENCE_LOGPROB = -1.0

sys.path.insert(0, str(SUBTITLE_GEN_DIR))

try:
    from mcp_client import load_terms, build_initial_prompt, get_correction, fuzzy_match
except ImportError:
    print("[WARN] Could not import subtitle-gen tools")
    load_terms = lambda: {}
    build_initial_prompt = lambda: ""
    get_correction = lambda x: None
    fuzzy_match = lambda x, threshold=75: None

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("[ERROR] faster-whisper not installed. Run: pip install faster-whisper")
    sys.exit(1)

def is_suspicious_word(word):
    """Check if word looks like a misheard term."""
    import re
    word_clean = word.strip(".,!?;:\"'()[]{}").lower()
    if len(word_clean) < 2:
        return False
    common = {"the", "a", "an", "is", "and", "or", "but", "that", "this"}
    if word_clean in common:
        return False
    if re.match(r"^[A-Z]{2,6}$", word.strip(".,!?;:\"'()[]{}")) and word_clean not in {"api", "url"}:
        return True
    if re.search(r"[a-z][A-Z]", word):
        return True
    if re.search(r"\d", word_clean) and re.search(r"[a-z]", word_clean):
        return True
    return False

def apply_correction(text, old_word, new_word):
    """Replace word preserving punctuation."""
    import re
    pattern = re.compile(re.escape(old_word), re.IGNORECASE)
    return pattern.sub(new_word, text)

def correct_transcript(segments):
    """Apply term corrections to segments."""
    import re
    corrections_made = []
    uncertain_found = []
    
    for seg in segments:
        original_text = seg["text"]
        corrected_text = original_text
        words = re.findall(r"\S+", original_text)
        
        for word in words:
            word_clean = word.strip(".,!?;:\"'()[]{}").lower()
            if len(word_clean) < 2:
                continue
            
            exact = get_correction(word_clean)
            if exact:
                corrected_text = apply_correction(corrected_text, word, exact)
                if word != exact:
                    corrections_made.append((word, exact, "exact"))
                continue
            
            if is_suspicious_word(word):
                fuzzy = fuzzy_match(word_clean, threshold=75) if fuzzy_match else None
                if fuzzy:
                    matched_key, correction, score = fuzzy
                    corrected_text = apply_correction(corrected_text, word, correction)
                    corrections_made.append((word, correction, f"fuzzy:{score:.0f}%"))
                else:
                    if word_clean not in [u[0] for u in uncertain_found]:
                        uncertain_found.append((word_clean, word, original_text[:50]))
        
        seg["text"] = corrected_text
    
    return segments, corrections_made, uncertain_found

def transcribe_audio(audio_path, model=None, output_dir=None, device=None):
    """Transcribe audio using faster-whisper."""
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    if model is None:
        model = WHISPER_MODEL
    
    if output_dir is None:
        output_dir = OUTPUT_DIR
    else:
        output_dir = Path(output_dir)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"[INFO] Transcribing with faster-whisper model: {model}")
    print(f"[INFO] Loading model...")
    
    whisper_device = device or WHISPER_DEVICE or "cpu"
    whisper_model = WhisperModel(model, device=whisper_device, compute_type="auto")
    
    try:
        terms_dict = load_terms()
        initial_prompt = build_initial_prompt()
        term_count = len(terms_dict.get("terms", {}))
        print(f"[INFO] Loaded {term_count} term mappings")
    except:
        print(f"[WARN] Could not load terms")
        initial_prompt = ""
    
    print("[INFO] Pass 1: Transcribing...")
    # Language is detected, not forced. Forcing "en" made non-English speech come out as invented English.
    segments, info = whisper_model.transcribe(
        str(audio_path),
        language=None,
        condition_on_previous_text=False,
        initial_prompt=initial_prompt if initial_prompt else None,
    )
    
    total_seconds = float(getattr(info, "duration", 0) or 0)
    collected = []
    logprobs = []
    for seg in segments:
        collected.append({"start": seg.start, "end": seg.end, "text": seg.text})
        # Confidence is a report, not a requirement: a missing value must never stop the transcript.
        avg_logprob = getattr(seg, "avg_logprob", None)
        if avg_logprob is not None:
            logprobs.append(float(avg_logprob))
        if total_seconds > 0:
            # Measured: audio seconds transcribed so far, out of the audio duration.
            report_progress(min(float(seg.end) / total_seconds, 1.0))
    segments = collected
    
    print("[INFO] Pass 2: Applying corrections...")
    segments, corrections_made, uncertain_found = correct_transcript(segments)
    
    full_text = " ".join([seg["text"] for seg in segments])
    
    if corrections_made:
        print(f"[INFO] Applied {len(corrections_made)} corrections")
    
    if uncertain_found:
        print(f"[INFO] Found {len(uncertain_found)} uncertain terms")
    
    # Save outputs
    transcript_path = output_dir / "transcript.txt"
    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(full_text)
    print(f"[DONE] Saved: {transcript_path}")

    # How reliable the transcript is. Shown on the results page; it never stops the job.
    mean_logprob = sum(logprobs) / len(logprobs) if logprobs else None
    notes = {
        "language": getattr(info, "language", None),
        "language_probability": round(float(getattr(info, "language_probability", 0) or 0), 3),
        "mean_logprob": None if mean_logprob is None else round(mean_logprob, 3),
        "low_confidence": mean_logprob is None or mean_logprob < LOW_CONFIDENCE_LOGPROB,
    }
    (output_dir / "transcript_notes.json").write_text(json.dumps(notes, indent=2), encoding="utf-8")
    print(f"[INFO] Transcript language: {notes['language']}, low confidence: {notes['low_confidence']}")
    
    segments_path = output_dir / "segments.json"
    with open(segments_path, "w", encoding="utf-8") as f:
        json.dump(segments, f, indent=2, ensure_ascii=False)
    print(f"[DONE] Saved: {segments_path}")
    
    if uncertain_found:
        uncertain_path = output_dir / "uncertain_terms.json"
        uncertain_data = {
            "terms": [{"word": w, "original": o, "context": c, "count": 1} for w, o, c in uncertain_found],
            "reviewed": [],
        }
        with open(uncertain_path, "w", encoding="utf-8") as f:
            json.dump(uncertain_data, f, indent=2, ensure_ascii=False)
        print(f"[INFO] Saved: {uncertain_path}")
    
    return {
        "text": full_text,
        "segments": segments,
        "corrections": len(corrections_made),
        "uncertain_terms": len(uncertain_found),
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Transcribe audio")
    parser.add_argument("audio_path", help="Path to audio file")
    parser.add_argument("--model", "-m", default=WHISPER_MODEL)
    parser.add_argument("--output", "-o", help="Output directory")
    args = parser.parse_args()
    
    result = transcribe_audio(args.audio_path, model=args.model, output_dir=args.output)
    print(f"\n[SUMMARY]")
    print(f"  Transcript: {len(result['text'])} chars, {len(result['segments'])} segments")
    print(f"  Corrections: {result['corrections']}")
    print(f"  Uncertain terms: {result['uncertain_terms']}")
