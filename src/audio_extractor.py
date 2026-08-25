"""Extract audio from video using FFmpeg."""

import subprocess
import sys
from pathlib import Path
from config import OUTPUT_DIR, AUDIO_FORMAT

def extract_audio(video_path, output_dir=None):
    """Extract audio from video file using FFmpeg."""
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    
    if output_dir is None:
        output_dir = OUTPUT_DIR
    else:
        output_dir = Path(output_dir)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / f"audio.{AUDIO_FORMAT}"
    
    print(f"[INFO] Extracting audio from: {video_path}")
    print(f"[INFO] Output: {audio_path}")
    
    try:
        cmd = [
            "ffmpeg",
            "-i", str(video_path),
            "-q:a", "5",
            "-map", "a",
            "-y",
            str(audio_path),
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        
        if result.returncode != 0:
            print(f"[ERROR] FFmpeg error: {result.stderr}")
            raise RuntimeError(f"FFmpeg extraction failed")
        
        if not audio_path.exists():
            raise RuntimeError("Audio extraction failed - no output file")
        
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        print(f"[DONE] Audio extracted: {audio_path} ({file_size_mb:.1f} MB)")
        return audio_path
    
    except FileNotFoundError:
        print("[ERROR] FFmpeg not found. Install with: brew install ffmpeg")
        sys.exit(1)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract audio from video")
    parser.add_argument("video_path", help="Path to video file")
    parser.add_argument("--output", "-o", help="Output directory")
    args = parser.parse_args()
    
    audio_path = extract_audio(args.video_path, args.output)
    print(f"\nAudio saved to: {audio_path}")
