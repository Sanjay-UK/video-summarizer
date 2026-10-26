"""Highlight rows: requested, successful, failed, not generated, and unrequested languages are kept distinct.

A language that was not requested never appears as Failed. Legacy metadata that lacks the language-selection field is
read conservatively: only languages with a status record are shown, and nothing is assumed about the rest.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import highlight_jobs as hj  # noqa: E402


class StatusFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.job = Path(tmp.name) / "job"
        self.job.mkdir()

    def _status(self, payload):
        path = self.job / "highlights" / "highlight_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _file(self, code):
        out = self.job / "highlights" / code / f"highlight_{code}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")

    def _rows(self):
        return {row["code"]: row for row in hj.list_highlight_language_status(self.job)}


class RequestedLanguageTests(StatusFixture):
    def test_1_requested_and_succeeded(self):
        self._status({"requested_languages": ["hi"], "languages": {"hi": {"status": "success"}}})
        self._file("hi")
        row = self._rows()["hi"]
        self.assertEqual(row["status"], "success")
        self.assertTrue(row["download_available"])

    def test_2_requested_and_failed_keeps_the_real_reason(self):
        self._status({"requested_languages": ["hi"], "languages": {"hi": {"status": "failed", "message": "render error"}}})
        row = self._rows()["hi"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["message"], "render error")
        self.assertFalse(row["download_available"])

    def test_3_not_requested_has_no_row_and_is_never_failed(self):
        self._status({"requested_languages": []})
        self.assertEqual(hj.list_highlight_language_status(self.job), [])

    def test_3b_an_unrequested_language_with_a_leftover_file_is_not_shown(self):
        self._status({"requested_languages": ["hi"], "languages": {"hi": {"status": "success"}}})
        self._file("hi")
        self._file("kn")
        self.assertEqual(set(self._rows()), {"hi"})

    def test_4_legacy_metadata_without_the_field_shows_only_attempted_languages(self):
        self._status({"languages": {"hi": {"status": "failed", "message": "old failure"}}})
        rows = self._rows()
        self.assertEqual(set(rows), {"hi"})
        self.assertEqual(rows["hi"]["status"], "failed")

    def test_4b_legacy_metadata_without_the_field_and_no_records_shows_nothing(self):
        self._status({})
        self.assertEqual(hj.list_highlight_language_status(self.job), [])

    def test_4c_legacy_list_with_no_record_is_not_generated_not_failed(self):
        self._status({"requested_languages": ["en"]})
        row = self._rows()["en"]
        self.assertEqual(row["status"], "not_generated")
        self.assertNotEqual(row["status"], "failed")

    def test_4d_a_success_record_without_its_file_is_treated_as_failed(self):
        self._status({"requested_languages": ["hi"], "languages": {"hi": {"status": "success"}}})
        self.assertEqual(self._rows()["hi"]["status"], "failed")

    def test_5_mixed_requested_and_unrequested_in_one_job(self):
        self._status({"requested_languages": ["hi", "kn", "ta"],
                      "languages": {"hi": {"status": "success"}, "kn": {"status": "failed", "message": "font"}}})
        self._file("hi")
        rows = self._rows()
        self.assertEqual({code: row["status"] for code, row in rows.items()},
                         {"hi": "success", "kn": "failed", "ta": "not_generated"})
        self.assertNotIn("te", rows, "a language that was not requested is absent")

    def test_failed_languages_for_retry_are_only_the_attempted_failures(self):
        self._status({"requested_languages": ["hi", "ta"], "languages": {"hi": {"status": "failed", "message": "x"}}})
        self.assertEqual(hj.failed_highlight_languages(self.job), ["hi"])


class ProcessRunTests(unittest.TestCase):
    """The job run records requested languages only when a highlight was asked for."""

    @classmethod
    def setUpClass(cls):
        from tests.test_progress_fixes import app_module

        if app_module is None:
            raise unittest.SkipTest("the Flask app is not part of the worker image")

    def _probe(self, highlight, executor_result=None):
        """Run a full job and capture what it writes. Files are read inside the run, before its temp folder is removed."""
        from tests.test_progress_fixes import FullRunTestBase, app_module

        captured = {}

        class Probe(FullRunTestBase):
            def runTest(self):
                if executor_result is not None:
                    self.executor.run_highlight_phase.return_value = executor_result
                job, ok = self._run(highlight=highlight)
                captured["ok"] = ok
                captured["status"] = json.loads(
                    (job.job_output_dir / "highlights" / "highlight_status.json").read_text(encoding="utf-8"))
                captured["api"] = app_module.app.test_client().get(f"/api/results/{job.job_id}").get_json()
                captured["rows"] = hj.list_highlight_language_status(job.job_output_dir)

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.errors + result.failures, [], result.errors + result.failures)
        self.assertTrue(captured.get("ok"))
        return captured

    def test_no_highlight_requested_records_no_languages_and_no_row(self):
        captured = self._probe(highlight=False)
        self.assertEqual(captured["status"]["requested_languages"], [])
        self.assertEqual(captured["api"].get("highlight_language_status", []), [])

    def test_requested_highlight_that_produces_nothing_is_failed_not_unknown(self):
        captured = self._probe(highlight=True, executor_result={})
        self.assertEqual(captured["status"]["requested_languages"], ["en"])
        row = [r for r in captured["rows"] if r["code"] == "en"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("No highlight file was produced", row["message"])


if __name__ == "__main__":
    unittest.main()
