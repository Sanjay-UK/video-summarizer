"""The History "Copy link" button: fetches a fresh highlight link, copies it, and does not open the job.

Node runs the real copyHighlightLink from static/js/app.js with fetch and the clipboard stubbed. Skipped without Node.
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
const a = src.indexOf('async function copyHighlightLink(');
const b = src.indexOf('function truncate(');
if (a < 0 || b < 0) { console.log(JSON.stringify({ error: 'copyHighlightLink not found' })); process.exit(0); }
const calls = [];
const copied = [];
const alerts = [];
const fetch = async (url) => { calls.push(url); return { ok: true, status: 200, json: async () => ({ url: 'https://signed.example/x?X-Amz-Signature=abc', expires_in: 3600 }) }; };
const navigator = { clipboard: { writeText: async (t) => { copied.push(t); } } };
const alert = (m) => alerts.push(m);
const setTimeout = (fn) => fn();
const run = new Function('fetch', 'navigator', 'alert', 'setTimeout',
  src.slice(a, b) + '; return copyHighlightLink;')(fetch, navigator, alert, setTimeout);
const button = { textContent: 'Copy link (EN)', disabled: false };
let stopped = false;
run({ stopPropagation: () => { stopped = true; }, currentTarget: button }, '0a1b2c3d', 'en').then(() => {
  console.log(JSON.stringify({ calls, copied, alerts, stopped, label: button.textContent }));
});
"""


@unittest.skipUnless(shutil.which("node"), "Node is not installed")
class CopyHighlightLinkTests(unittest.TestCase):
    def _run(self):
        out = subprocess.run(["node", "-e", HARNESS, str(APP_JS)], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_fetches_the_fresh_link_for_that_job_and_language(self):
        self.assertEqual(self._run()["calls"], ["/api/highlight-url/0a1b2c3d/en"])

    def test_copies_the_signed_url_and_does_not_open_the_job(self):
        result = self._run()
        self.assertEqual(result["copied"], ["https://signed.example/x?X-Amz-Signature=abc"])
        self.assertTrue(result["stopped"], "the click must not reach the History item")
        self.assertEqual(result["alerts"], [])

    def test_history_items_render_one_copy_button_per_highlight(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("copyHighlightLink(event, '${escapeHtml(job.job_id)}', '${escapeHtml(h.code)}')", source)


if __name__ == "__main__":
    unittest.main()
