"""Regression tests: a remote highlight failure must be reported, never replaced by a silent local render.

Also covers the summary_segments.json artifact that subtitles depend on.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "")

from src import pipeline_executor  # noqa: E402
from src.user_messages import GENERIC_HIGHLIGHT_FAILURE  # noqa: E402
from src.highlight_jobs import highlight_file_for_lang, list_highlight_language_status  # noqa: E402

PATCH_BASE = "src.pipeline_executor.PipelineExecutor.run_highlight_phase"


class RemoteHighlightFailureTests(unittest.TestCase):
    def _run(self, *, remote_side_effect=None, write_mp4_for=None):
        executor = pipeline_executor.ServerlessRunpodPipelineExecutor()
        tmp = tempfile.TemporaryDirectory()
        job_dir = Path(tmp.name)

        def fake_remote(**kwargs):
            if remote_side_effect is not None:
                raise remote_side_effect
            for lang in write_mp4_for or []:
                mp4 = highlight_file_for_lang(job_dir, lang)
                mp4.parent.mkdir(parents=True, exist_ok=True)
                mp4.write_bytes(b"fake mp4 from worker")
            return {}

        logs = []
        patches = [
            mock.patch("src.serverless_model_catalog.serverless_highlight_on_worker", return_value=True),
            mock.patch("src.runpod_serverless.load_endpoint_id_from_selection", return_value="ep"),
            mock.patch("src.runpod_serverless.run_highlight_phase_via_serverless", side_effect=fake_remote),
        ]
        local_render = mock.patch(PATCH_BASE, return_value=Path("/should/not/be/used.mp4"))
        for p in patches:
            p.start()
        local = local_render.start()
        try:
            result = executor.run_highlight_phase(
                job_id="job1",
                video_path=job_dir / "video.mp4",
                segments=[],
                job_output_dir=job_dir,
                summarization_model="llama3.2:3b",
                compression="medium",
                languages=["en"],
                log=logs.append,
            )
            statuses = list_highlight_language_status(job_dir)
        finally:
            local_render.stop()
            for p in patches:
                p.stop()
        return result, statuses, local, logs, tmp

    def test_remote_exception_is_reported_not_rendered_locally(self):
        result, statuses, local, logs, tmp = self._run(remote_side_effect=RuntimeError("worker crashed"))
        self.addCleanup(tmp.cleanup)
        self.assertFalse(local.called, "local FFmpeg fallback must not run after a remote failure")
        self.assertEqual(result, {}, "no highlight path may be returned for a failed remote render")
        en = [s for s in statuses if s["code"] == "en"][0]
        self.assertEqual(en["status"], "failed")
        self.assertIn(GENERIC_HIGHLIGHT_FAILURE, en["message"])
        self.assertNotIn("worker crashed", en["message"], "the user must not see the worker error")
        self.assertTrue(any("worker crashed" in line for line in logs), "the detail stays in the log for debugging")

    def test_remote_success_without_file_is_reported_as_failure(self):
        result, statuses, local, logs, tmp = self._run(write_mp4_for=[])
        self.addCleanup(tmp.cleanup)
        self.assertFalse(local.called)
        self.assertEqual(result, {})
        en = [s for s in statuses if s["code"] == "en"][0]
        self.assertEqual(en["status"], "failed")
        self.assertIn(GENERIC_HIGHLIGHT_FAILURE, en["message"])
        self.assertTrue(any("No highlight file" in line for line in logs))

    def test_remote_success_with_file_returns_worker_output(self):
        result, statuses, local, logs, tmp = self._run(write_mp4_for=["en"])
        self.addCleanup(tmp.cleanup)
        self.assertFalse(local.called)
        self.assertIn("en", result)
        self.assertTrue(result["en"].is_file())


@unittest.skipUnless(importlib.util.find_spec("runpod"), "worker dependencies (runpod) not installed here")
class WorkerSummarySegmentsTests(unittest.TestCase):
    def test_artifact_writer_saves_summary_segments(self):
        from src.runpod_serverless import _write_artifacts_from_output

        segments = [{"start": 0.0, "end": 2.5, "text": "Hello", "summary_text": "Hello."}]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write_artifacts_from_output(out, {"summary_segments_json": json.dumps(segments)})
            written = json.loads((out / "summary_segments.json").read_text(encoding="utf-8"))
        self.assertEqual(written, segments)

    def test_heavy_phase_returns_summary_segments(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runpod_worker"))
        import handler  # noqa: E402

        def fake_heavy(workdir, manifest):
            (workdir / "segments.json").write_text("[]", encoding="utf-8")
            (workdir / "transcript.txt").write_text("t", encoding="utf-8")
            (workdir / "summary.txt").write_text("s", encoding="utf-8")
            (workdir / "heavy_done.json").write_text("{}", encoding="utf-8")
            (workdir / "summary_segments.json").write_text('[{"start": 0}]', encoding="utf-8")

        with mock.patch.object(handler, "_warm_ollama"), mock.patch.object(
            handler, "decode_audio_to_path"
        ), mock.patch.object(handler, "run_heavy_phase", side_effect=fake_heavy):
            out = handler._heavy_phase({"audio_file": "audio.mp3"})
        self.assertEqual(out["summary_segments_json"], '[{"start": 0}]')


if __name__ == "__main__":
    unittest.main()
