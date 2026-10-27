"""How the page labels one highlight language: success, not generated, and failed are kept apart.

Node runs the real highlightRowView from static/js/app.js. Skipped without Node.
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
const a = src.indexOf('function highlightRowView(');
const b = src.indexOf('function renderHighlightDownloads(');
if (a < 0 || b < 0) { console.log(JSON.stringify({ error: 'highlightRowView not found' })); process.exit(0); }
const highlightRowView = new Function(src.slice(a, b) + '; return highlightRowView;')();
const rows = {
  success: { status: 'success', download_available: true, message: '' },
  failed: { status: 'failed', download_available: false, message: 'render error' },
  failedNoMessage: { status: 'failed', download_available: false, message: '' },
  notGenerated: { status: 'not_generated', download_available: false, message: '' },
};
const out = {};
for (const [name, row] of Object.entries(rows)) out[name] = highlightRowView(row);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class HighlightRowViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["node", "-e", HARNESS, str(APP_JS)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.v = json.loads(proc.stdout.strip().splitlines()[-1])
        if "error" in cls.v:
            raise AssertionError(cls.v["error"])

    def test_success_is_a_check_with_no_failure_text(self):
        self.assertTrue(self.v["success"]["ok"])
        self.assertEqual(self.v["success"]["symbol"], "✓")

    def test_failed_shows_the_real_reason(self):
        self.assertFalse(self.v["failed"]["ok"])
        self.assertEqual(self.v["failed"]["detail"], "render error")

    def test_failed_without_a_message_says_failed(self):
        self.assertEqual(self.v["failedNoMessage"]["detail"], "Failed")

    def test_not_generated_is_neutral_and_never_called_failed(self):
        view = self.v["notGenerated"]
        self.assertEqual(view["detail"], "Not generated")
        self.assertEqual(view["symbol"], "○")
        self.assertNotIn("Failed", view["detail"])
        self.assertNotEqual(view["background"], self.v["failed"]["background"])


if __name__ == "__main__":
    unittest.main()
