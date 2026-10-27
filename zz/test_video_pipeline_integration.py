"""Integration test: one video job through the real local pipeline, on a small synthetic video.

Runs for real: process_video job orchestration and state, FFmpeg audio extraction and probing, transcript
correction and file writing, summarize_transcript (three passes), audio explanation and subtitle files, highlight
topic assembly, scene frame extraction, scene muxing with burned-in captions, and the final highlight MP4.

Replaced (external services only):
- Whisper model weights: a fake faster-whisper model returns fixed segments. Nothing is downloaded.
- Ollama server: a fake client answers each prompt type with fixed text. No LLM is called.
- Edge TTS network call: replaced by an FFmpeg-generated tone of a set length. No speech is synthesized.

Not covered: YouTube download, RunPod/GPU workers, translation to other languages (Google), speech quality, and the
Flask HTTP layer. The test needs an FFmpeg build with libass (the subtitles filter) and is skipped without one.
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

import fake_s3

import fake_s3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "")

import app as app_module  # noqa: E402
from src import highlight_ffmpeg as hf  # noqa: E402
from src.highlight_tts import TtsResult  # noqa: E402

LOCAL_RUNTIME = {"mode": "local", "location_label": "this machine", "heavy_location": "local",
                 "light_location": "local"}

SEGMENTS = [
    (0.0, 3.0, "First, rinse the sushi rice until the water runs clear."),
    (3.0, 6.0, "Then cook the rice and let it rest for ten minutes."),
    (6.0, 9.0, "Season the rice with vinegar, sugar, and salt."),
    (9.0, 12.0, "Shape the rice by hand and add fish or vegetables."),
]
CONCEPTS = """**Ranked Concepts:**

[CRITICAL]
* Rice preparation: rinsing and cooking the rice.
* Seasoning: vinegar, sugar and salt.

[IMPORTANT]
* Shaping: hand shaping with fish or vegetables.
"""
SUMMARY = "Rinse and cook the sushi rice, season it with vinegar, sugar and salt, then shape it by hand."
NARRATION = "This step prepares the rice."
TOPICS = {
    "topics": [
        {"segment_id": 0, "topic_title": "Rice preparation", "summary_text": "Rinse and cook the rice."},
        {"segment_id": 2, "topic_title": "Seasoning", "summary_text": "Season the rice with vinegar, sugar and salt."},
        {"segment_id": 3, "topic_title": "Shaping", "summary_text": "Shape the rice by hand."},
    ]
}


def _libass_ffmpeg():
    try:
        binary = hf.resolve_ffmpeg_bin()
    except Exception:  # noqa: BLE001
        return None
    proc = subprocess.run([binary, "-hide_banner", "-filters"], capture_output=True, text=True)
    return binary if " subtitles " in proc.stdout else None


FFMPEG = _libass_ffmpeg()


def _run_ffmpeg(args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _make_video(path: Path):
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=12",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])


def _write_tone(path: Path, seconds: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                 "-c:a", "libmp3lame", str(path)])


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        [hf.resolve_ffprobe_bin(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


class FakeWhisperModel:
    """Stands in for the faster-whisper model weights only. transcribe() returns fixed segments."""

    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, audio_path, **kwargs):
        segs = [SimpleNamespace(start=s, end=e, text=t) for s, e, t in SEGMENTS]
        return iter(segs), SimpleNamespace(duration=12.0)


class FailingWhisperModel(FakeWhisperModel):
    def __init__(self, *args, **kwargs):
        raise RuntimeError("whisper model weights could not be loaded")


class FakeOllamaClient:
    """Stands in for the Ollama server. Each prompt type gets a fixed answer; no model runs."""

    def __init__(self, *args, **kwargs):
        pass

    def generate(self, *args, **kwargs):
        prompt = kwargs.get("prompt", "")
        if kwargs.get("format") == "json":
            text = json.dumps(TOPICS)
        elif "identifying educational concepts" in prompt:
            text = CONCEPTS
        elif "educational summarizer" in prompt:
            text = SUMMARY
        else:
            text = NARRATION
        return {"response": text}


async def fake_generate_audio_async(ssml_text, output_path):
    _write_tone(Path(output_path), 3.0)
    return Path(output_path)


async def fake_synthesize(text, output_path, *, voice):
    _write_tone(Path(output_path), 2.0)
    return TtsResult(audio_path=Path(output_path), spoken_text=text,
                     duration_seconds=2.0, boundary_kind="none", boundaries=[], voice_used=voice)


def _start_environment(stack: contextlib.ExitStack, output_dir: Path, jobs: dict, whisper_cls):
    """Returns the fake S3 store the run writes to."""
    store = fake_s3.new_store()
    """Route process_video into a temp output folder and swap only the external services."""
    stack.enter_context(mock.patch.object(app_module, "OUTPUT_DIR", output_dir))
    stack.enter_context(mock.patch.object(app_module, "jobs", jobs))
    stack.enter_context(mock.patch.object(app_module, "_s3_store", return_value=store))
    stack.enter_context(mock.patch.object(app_module, "resolve_runtime_for_job", return_value=LOCAL_RUNTIME))
    stack.enter_context(mock.patch.object(app_module, "apply_runtime_side_effects", return_value={}))
    stack.enter_context(mock.patch.object(app_module, "restore_runtime_side_effects", return_value=None))
    stack.enter_context(mock.patch("src.transcriber.WhisperModel", whisper_cls))
    stack.enter_context(mock.patch("ollama.Client", FakeOllamaClient))
    stack.enter_context(mock.patch("src.tts_generator.generate_audio_async", fake_generate_audio_async))
    stack.enter_context(mock.patch("src.highlight_tts._synthesize_async", fake_synthesize))
    return store


@unittest.skipUnless(FFMPEG, "needs an FFmpeg build with libass (subtitles filter) on this machine")
class CompletedJobIntegrationTests(unittest.TestCase):
    """One real run of process_video on a synthetic video. Every test reads the files it produced."""

    JOB_ID = "a1b2c3d4"

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cls.video = root / "lecture.mp4"
        _make_video(cls.video)
        cls.out = root / "output"
        cls.out.mkdir()
        cls.jobs = {}
        stack = contextlib.ExitStack()
        cls.addClassCleanup(stack.close)
        cls.store = _start_environment(stack, cls.out, cls.jobs, FakeWhisperModel)

        cls.job = app_module.JobProgress(cls.JOB_ID, str(cls.video))
        cls.jobs[cls.JOB_ID] = cls.job
        cls.ok = app_module.process_video(cls.JOB_ID, str(cls.video), highlight_video=True)
        cls.job_dir = cls.out / cls.JOB_ID

    def test_job_completes_and_reports_success(self):
        self.assertTrue(self.ok, "pipeline failed: " + " | ".join(self.job.log_lines[-5:]))
        snap = self.job.to_dict()
        self.assertEqual(snap["status"], "completed")
        self.assertEqual(snap["progress"], 1.0)
        self.assertIsNone(snap["error"])
        self.assertTrue(self.job.results["highlight_video"])
        self.assertTrue(self.job.results["has_audio"])

    def test_transcript_and_segments_come_from_the_audio(self):
        transcript = (self.job_dir / "transcript.txt").read_text(encoding="utf-8")
        self.assertIn("rinse the sushi rice", transcript.lower())
        segments = json.loads((self.job_dir / "segments.json").read_text(encoding="utf-8"))
        self.assertEqual(len(segments), len(SEGMENTS))
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[-1]["end"], 12.0)

    def test_summary_is_written_and_linked_to_timestamps(self):
        summary = (self.job_dir / "summary.txt").read_text(encoding="utf-8")
        self.assertIn("vinegar, sugar and salt", summary)
        linked = json.loads((self.job_dir / "summary_segments.json").read_text(encoding="utf-8"))
        self.assertTrue(linked, "summary_segments.json has no entries")
        manifest = fake_s3.manifest(self.store, self.JOB_ID)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["results"]["summary_length"], len(summary))
        self.assertIn("summary.txt", manifest["files"])
        self.assertIn("source.mp4", manifest["files"], "the source video is kept in S3 with the job")

    def test_narration_audio_is_playable_and_matches_the_subtitles(self):
        probe = _ffprobe(self.job_dir / "audio_explanation.mp3")
        self.assertGreater(float(probe["format"]["duration"]), 1.0)
        audio_text = (self.job_dir / "audio_transcript.txt").read_text(encoding="utf-8")
        self.assertIn("vinegar", audio_text.lower())
        cues = [b for b in (self.job_dir / "summary.srt").read_text(encoding="utf-8").split("\n\n") if b.strip()]
        self.assertTrue(cues, "summary.srt has no cues")
        self.assertTrue(all("-->" in c for c in cues))
        self.assertIn("vinegar", " ".join(cues).lower())

    def test_highlight_video_is_720p_with_an_audio_track(self):
        highlight = self.job_dir / "highlights" / "en" / "highlight_en.mp4"
        self.assertTrue(highlight.is_file(), "highlight_en.mp4 was not written")
        probe = _ffprobe(highlight)
        video = [s for s in probe["streams"] if s["codec_type"] == "video"]
        audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        self.assertEqual(len(video), 1)
        self.assertEqual((video[0]["width"], video[0]["height"]), (1280, 720))
        self.assertEqual(len(audio), 1)
        self.assertGreater(float(probe["format"]["duration"]), 2.0)
        self.assertEqual(self.job.results["highlight_languages"][0]["code"], "en")


@unittest.skipUnless(FFMPEG, "needs an FFmpeg build with libass (subtitles filter) on this machine")
class FailedTranscriptionIntegrationTests(unittest.TestCase):
    """If Whisper cannot load, the job fails with the real reason and writes no completed outputs."""

    JOB_ID = "e5f6a7b8"

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cls.video = root / "lecture.mp4"
        _make_video(cls.video)
        cls.out = root / "output"
        cls.out.mkdir()
        cls.jobs = {}
        stack = contextlib.ExitStack()
        cls.addClassCleanup(stack.close)
        cls.store = _start_environment(stack, cls.out, cls.jobs, FailingWhisperModel)

        cls.job = app_module.JobProgress(cls.JOB_ID, str(cls.video))
        cls.jobs[cls.JOB_ID] = cls.job
        cls.ok = app_module.process_video(cls.JOB_ID, str(cls.video), highlight_video=True)
        cls.job_dir = cls.out / cls.JOB_ID

    def test_failure_is_reported_with_its_real_reason(self):
        self.assertFalse(self.ok)
        snap = self.job.to_dict()
        self.assertEqual(snap["status"], "error")
        self.assertIn("whisper model weights could not be loaded", snap["error"])

    def test_no_summary_or_highlight_is_written_after_the_failure(self):
        self.assertFalse((self.job_dir / "summary.txt").exists())
        self.assertFalse((self.job_dir / "highlights" / "en" / "highlight_en.mp4").exists())
        manifest = fake_s3.manifest(self.store, self.JOB_ID)
        self.assertEqual(manifest["status"], "error")
        self.assertNotIn("summary.txt", manifest["files"])


if __name__ == "__main__":
    unittest.main()
