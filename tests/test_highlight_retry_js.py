"""Playback retry limit for highlight links, run against the real bindHighlightSource in static/js/app.js.

Node runs a small harness with stub fetch and media objects, so the test counts real link requests and
decides the outcome without a browser. Skipped when Node is not installed.
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "js" / "app.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function bindHighlightSource(');
const end = src.indexOf('function unbindHighlightSource(');
if (start < 0 || end < 0) { console.log(JSON.stringify({ error: 'bindHighlightSource not found' })); process.exit(0); }
let fetches = 0;
const errors = [];
const fetchHighlightUrl = async () => { fetches += 1; return 'https://fake.example/link' + fetches; };
const showHighlightPlayerError = (m) => { if (m) errors.push(m); };  // empty text clears the message
const bind = new Function('fetchHighlightUrl', 'showHighlightPlayerError',
  src.slice(start, end) + '; return bindHighlightSource;')(fetchHighlightUrl, showHighlightPlayerError);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
(async () => {
  const media = { src: '', loads: 0, onerror: null };
  media.load = () => { media.loads += 1; };
  bind(media, media, 'abcd1234', 'en');
  await flush();
  const afterOpen = { fetches, loads: media.loads, errors: errors.length };
  // Loading data successfully must not reset the retry budget.
  if (typeof media.onloadeddata === 'function') media.onloadeddata();
  media.onerror(); await flush();              // first failure: one fresh link
  const afterFirstFailure = { fetches, loads: media.loads, errors: errors.length };
  // The refreshed link loads, then fails again within the same attempt. Loading must not reset the budget.
  if (typeof media.onloadeddata === 'function') media.onloadeddata();
  media.onerror(); await flush();              // second failure in the same attempt: no more fetches
  media.onerror(); await flush();              // repeated failures: still no more fetches
  console.log(JSON.stringify({
    afterOpen, afterFirstFailure,
    final: { fetches, loads: media.loads, errors: errors.length, lastError: errors[errors.length - 1] || '' },
  }));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class HighlightRetryLimitTests(unittest.TestCase):
    def setUp(self):
        proc = subprocess.run(
            ["node", "-e", HARNESS, str(APP_JS)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.result = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_opening_a_job_requests_one_link_and_loads_it(self):
        self.assertEqual(self.result["afterOpen"], {"fetches": 1, "loads": 1, "errors": 0})

    def test_one_failure_gets_exactly_one_fresh_link(self):
        self.assertEqual(self.result["afterFirstFailure"]["fetches"], 2)
        self.assertEqual(self.result["afterFirstFailure"]["loads"], 2)

    def test_repeated_failures_never_refresh_again_and_show_an_error(self):
        final = self.result["final"]
        self.assertEqual(final["fetches"], 2, "the retry must stop after one refresh")
        self.assertGreaterEqual(final["errors"], 1)
        self.assertIn("Reopen the job", final["lastError"])


if __name__ == "__main__":
    sys.exit(unittest.main())
