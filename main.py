#!/usr/bin/env python3
"""
Main CLI for automatic video summarization pipeline.

Usage:
 python3 main.py "https://youtube.com/watch?v=..." [--quality 360]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DOWNLOADS_DIR, OUTPUT_DIR
from src.audio_extractor import extract_audio
from src.downloader import download_youtube_video
from src.subtitle_generator import generate_subtitles
from src.summarizer import summarize_transcript, test_ollama
from src.transcriber import transcribe_audio
from src.tts_generator import generate_audio_explanation


def run_full_pipeline(url: str, quality: int = 360, model: str = "llama2") -> dict:
    """Run the complete video summarization pipeline."""
    print("=" * 70)
    print("AUTOMATIC VIDEO SUMMARIZATION PIPELINE")
    print("=" * 70)
    print()

    outputs = {}
    transcript_len = 0

    print("[WEEK 1] Downloading & Transcription")
    print("-" * 70)

    try:
        print("Step 1: Download YouTube video...")
        video_path = download_youtube_video(url, DOWNLOADS_DIR, quality=quality)
        outputs["video"] = video_path
        print()

        print("Step 2: Extract audio...")
        audio_path = extract_audio(video_path, OUTPUT_DIR)
        outputs["audio"] = audio_path
        print()

        print("Step 3: Transcribe with faster-whisper...")
        transcribe_audio(audio_path, model="base", output_dir=OUTPUT_DIR)
        outputs["transcript"] = OUTPUT_DIR / "transcript.txt"
        print()
    except Exception as e:
        print(f" Week 1 failed: {e}")
        return outputs

    print("[WEEK 2] Summarization with Ollama/Llama")
    print("-" * 70)

    try:
        print("Step 4: Summarize transcript...")
        summarize_transcript(
            transcript_path=OUTPUT_DIR / "transcript.txt",
            segments_path=OUTPUT_DIR / "segments.json",
            output_dir=OUTPUT_DIR,
            model=model,
        )
        outputs["summary"] = OUTPUT_DIR / "summary.txt"
        print()
    except Exception as e:
        print(f" Week 2 failed: {e}")
        print(" (Make sure Ollama is running: ollama serve)")
        return outputs

    print("[WEEK 3] Output Generation")
    print("-" * 70)

    try:
        print("Step 5: Generate subtitles...")
        srt_path = generate_subtitles(
            summary_segments_path=OUTPUT_DIR / "summary_segments.json",
            output_path=OUTPUT_DIR / "summary.srt",
        )
        outputs["subtitles"] = srt_path
        print()

        print("Step 6: Generate audio explanation...")
        summary_text = (OUTPUT_DIR / "summary.txt").read_text(encoding="utf-8")
        audio_exp_path, _audio_transcript = generate_audio_explanation(
            summary_text=summary_text,
            output_path=OUTPUT_DIR / "audio_explanation.mp3",
        )
        outputs["audio_explanation"] = audio_exp_path
        print()
    except Exception as e:
        print(f" Week 3 partial: {e}")
        print(" (Subtitles and audio may not have been generated)")

    print("=" * 70)
    print(" PIPELINE COMPLETE")
    print("=" * 70)
    print()
    print("Generated Files:")
    print()
    for name, path in outputs.items():
        if isinstance(path, Path) and path.exists():
            size = path.stat().st_size
            if size > 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            elif size > 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} bytes"
            print(f" {name:20} {path.name:40} ({size_str})")

    print()
    print("Summary Statistics:")
    if "transcript" in outputs and Path(outputs["transcript"]).exists():
        transcript_len = len(Path(outputs["transcript"]).read_text(encoding="utf-8"))
        print(f" Original transcript: {transcript_len:,} characters")

    if "summary" in outputs and Path(outputs["summary"]).exists():
        summary_len = len(Path(outputs["summary"]).read_text(encoding="utf-8"))
        print(f" Summary: {summary_len:,} characters")
        if transcript_len > 0:
            ratio = summary_len / transcript_len
            print(f" Compression: {ratio:.1%} of original")

    print()
    return outputs


def main():
    parser = argparse.ArgumentParser(
        description="Automatic Video Summarization Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
 python3 main.py "https://www.youtube.com/watch?v=fWjsdhR3z3c"
 python3 main.py "https://..." --quality 720 --model llama3.1
 python3 main.py --test-ollama
""",
    )
    parser.add_argument("url", nargs="?", help="YouTube video URL")
    parser.add_argument("--quality", "-q", type=int, default=360)
    parser.add_argument("--model", "-m", default="llama2")
    parser.add_argument("--test-ollama", action="store_true")

    args = parser.parse_args()

    if args.test_ollama:
        print("Testing Ollama connection...")
        if test_ollama():
            print("\n Ollama is ready!")
            sys.exit(0)
        print("\n Ollama connection failed!")
        sys.exit(1)

    if not args.url:
        parser.print_help()
        sys.exit(1)

    try:
        outputs = run_full_pipeline(args.url, quality=args.quality, model=args.model)
        if outputs:
            print("\n All steps completed successfully!")
            sys.exit(0)
        print("\n Pipeline failed!")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n Pipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
