"""Regression tests: the heavy RunPod job's metrics path after submission.

Commit 96c6f78 referenced a variable that no longer existed after submission moved into submit_and_wait, so a
successful heavy job raised NameError before its metrics were saved. These tests exercise run_heavy_phase_via_serverless
with RunPod submission and polling mocked, and check the success path, the processing-failure path, and the
submission-failure path, including lease release on every path.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import endpoint_capacity as ec  # noqa: E402
from src import runpod_serverless as rs  # noqa: E402

EP = "4jxzzc08sr5z0w"
GOOD_OUTPUT = {
    "transcript": "hello world",
    "segments_json": "[]",
    "summary": "a summary",
    "summary_translations_json": "{}",
    "summary_segments_json": "[]",
    "heavy_done_json": "{}",
}


class HeavyMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lease_dir = self.root / "leases"
        self.job_dir = self.root / "job"
        self.job_dir.mkdir()
        self.logs = []
        # RunPod is never contacted: capacity calls and submission are mocked.
        patches = [
            mock.patch.object(ec, "LEASE_DIR", self.lease_dir),
            mock.patch.object(rs, "patch_serverless_endpoint", return_value={}),
            mock.patch.object(rs, "get_serverless_endpoint", return_value=({"workers": {"max": 0}}, None)),
            mock.patch.object(rs, "build_heavy_phase_input", return_value={"job_type": "heavy_phase"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.audio = self.root / "audio.mp3"
        self.audio.write_bytes(b"audio")

    def _run(self):
        return rs.run_heavy_phase_via_serverless(
            endpoint_id=EP,
            job_id="app-job",
            audio_path=self.audio,
            job_output_dir=self.job_dir,
            whisper_model="base",
            summarization_model="llama3.2:3b",
            compression="medium",
            highlight_languages="en",
            log=self.logs.append,
        )

    def _leases_left(self):
        path = self.lease_dir / f"{ec._safe_name(EP)}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def test_success_saves_metrics_with_the_submitted_job_id(self):
        status = {"status": "COMPLETED", "output": dict(GOOD_OUTPUT), "delayTime": 1200, "executionTime": 3400}
        with mock.patch.object(rs, "submit_job_when_ready", return_value="job-42") as submit, mock.patch.object(
            rs, "poll_job_until_done", return_value=status
        ) as poll:
            result = self._run()  # must not raise NameError
        submit.assert_called_once()
        poll.assert_called_once()
        self.assertEqual(poll.call_args.args[1], "job-42", "polling must use the submitted job ID")
        metrics = json.loads((self.job_dir / "runpod_job_metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["runpod_job_id"], "job-42")
        self.assertEqual(metrics["delayTime"], 1200)
        self.assertEqual(metrics["executionTime"], 3400)
        self.assertEqual(result["id"], "job-42")
        self.assertEqual(self._leases_left(), [], "the job lease is released on success")

    def test_processing_failure_raises_the_real_error_and_releases_the_lease(self):
        status = {"status": "COMPLETED", "output": None}  # completed without output: a processing failure
        with mock.patch.object(rs, "submit_job_when_ready", return_value="job-7"), mock.patch.object(
            rs, "poll_job_until_done", return_value=status
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._run()
        self.assertIn("no output", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, NameError)
        self.assertEqual(self._leases_left(), [], "a processing failure must still release the lease")
        self.assertFalse((self.job_dir / "runpod_job_metrics.json").exists())

    def test_submission_failure_releases_the_lease_and_is_not_masked(self):
        with mock.patch.object(rs, "submit_job_when_ready", side_effect=rs.EndpointNotReadyError("still paused")):
            with self.assertRaises(rs.EndpointNotReadyError):
                self._run()
        self.assertEqual(self._leases_left(), [])


if __name__ == "__main__":
    unittest.main()
