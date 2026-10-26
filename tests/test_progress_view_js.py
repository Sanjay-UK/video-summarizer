"""The progress bar's view rules, run against the real progressViewFor in static/js/app.js.

Node loads the function and calls it with status payloads. The rules: confirmed values only, no backward movement
within a job, no 100 before completion, and no number for an indeterminate stage. Skipped without Node.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "js" / "app.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('const STEP_LABELS = {');
const end = src.indexOf('function updateProgressUI(');
if (start < 0 || end < 0) { console.log(JSON.stringify({ error: 'progressViewFor not found' })); process.exit(0); }
const progressViewFor = new Function(src.slice(start, end) + '; return progressViewFor;')();
const cases = {
  processing_20: progressViewFor({ status: 'processing', progress: 0.2 }, 0),
  backward_ignored: progressViewFor({ status: 'processing', progress: 0.1 }, 20),
  forward_stage: progressViewFor({ status: 'processing', progress: 0.5 }, 20),
  capped_before_completion: progressViewFor({ status: 'processing', progress: 1.0 }, 0),
  completed: progressViewFor({ status: 'completed', progress: 1.0 }, 50),
  error_keeps_last: progressViewFor({ status: 'error', progress: 0.4 }, 40),
  indeterminate_no_number: progressViewFor({ status: 'processing', progress: 0.35, indeterminate: true, current_step: 'transcribing' }, 35),
  indeterminate_keeps_previous: progressViewFor({ status: 'processing', progress: 0.85, indeterminate: true }, 35),
  garbage_progress: progressViewFor({ status: 'processing', progress: 'x' }, 10),
};
console.log(JSON.stringify(cases));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ProgressViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["node", "-e", HARNESS, str(APP_JS)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.v = json.loads(proc.stdout.strip().splitlines()[-1])
        if "error" in cls.v:
            raise AssertionError(cls.v["error"])

    def test_shows_confirmed_backend_progress(self):
        self.assertEqual(self.v["processing_20"]["percent"], 20)
        self.assertFalse(self.v["processing_20"]["indeterminate"])

    def test_never_moves_backward_within_a_job(self):
        self.assertEqual(self.v["backward_ignored"]["percent"], 20)

    def test_moves_forward_at_a_stage_change(self):
        self.assertEqual(self.v["forward_stage"]["percent"], 50)

    def test_never_shows_one_hundred_before_completion(self):
        self.assertEqual(self.v["capped_before_completion"]["percent"], 99)

    def test_completed_job_shows_one_hundred(self):
        self.assertEqual(self.v["completed"]["percent"], 100)
        self.assertEqual(self.v["completed"]["label"], "Completed")

    def test_failed_job_keeps_the_last_confirmed_value_and_says_failed(self):
        self.assertEqual(self.v["error_keeps_last"]["percent"], 40)
        self.assertEqual(self.v["error_keeps_last"]["label"], "Failed")

    def test_indeterminate_stage_has_no_invented_number(self):
        view = self.v["indeterminate_no_number"]
        self.assertTrue(view["indeterminate"])
        self.assertEqual(view["percent"], 35, "the last confirmed value is kept, not a guess")

    def test_indeterminate_stage_does_not_advance_the_number(self):
        self.assertEqual(self.v["indeterminate_keeps_previous"]["percent"], 35)

    def test_invalid_progress_values_are_treated_as_zero(self):
        self.assertEqual(self.v["garbage_progress"]["percent"], 10)


if __name__ == "__main__":
    unittest.main()
