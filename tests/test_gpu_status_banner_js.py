"""The JavaScript deployment banner (used while polling) follows the same rules as the server-rendered one.

Node loads the real renderer from static/js/deploy_status.js and renders fixed statuses. Checks: state labels,
and that endpoint, volume, and timing details appear only inside 'Advanced details'. Skipped when Node is missing.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_JS = ROOT / "static" / "js" / "deploy_status.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const pick = (from, to) => src.slice(src.indexOf(from), src.indexOf(to));
const body = pick('  function esc(s) {', '  function mountDeployStatus(') + '; return renderDeployStatusBanner;';
const render = new Function(body)();
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {};
for (const [name, c] of Object.entries(cases)) out[name] = render(c.status, c.meta);
console.log(JSON.stringify(out));
"""

ENDPOINT_ID = "EP-TEST-ENDPOINT-123"
VOLUME_ID = "VOL-TEST-VOLUME-456"


IDLE_LIVE = {
    "key": "idle", "label": "Idle — no workers running", "tone": "gray",
    "explanation": "No workers are running. Zero workers is normal: serverless workers scale to zero when idle.",
    "max_workers": 3, "workers": {"idle": 0, "running": 0}, "jobs": {"inQueue": 0, "inProgress": 0},
    "checked_at": "2026-01-01T00:00:00Z",
}
UNAVAILABLE_LIVE = {
    "key": "unavailable", "label": "Status unavailable", "tone": "amber",
    "explanation": "Could not read the endpoint settings from RunPod. Nothing has been changed.",
    "max_workers": None, "workers": None, "jobs": None, "checked_at": "2026-01-01T00:00:00Z",
}


def _status(**overrides):
    status = {
        "show_deploy_banner": True,
        "live": IDLE_LIVE,
        "phase": "ready",
        "compute_mode": "serverless",
        "hardware": {"name": "RTX A5000", "vram_gb": 24, "price_per_hour": 0.27},
        "endpoint_id": ENDPOINT_ID,
        "network_volume_id": VOLUME_ID,
        "workers_min": 0,
        "workers_max": 3,
        "last_delay_time_ms": 64455,
        "last_execution_time_ms": 73012,
        "can_stop": True,
        "can_delete_volume": True,
        "stop_action_label": "Stop serverless",
        "console_url": "https://www.runpod.io/console/serverless/user/endpoint/" + ENDPOINT_ID,
        "data_center_id": "AP-IN-1",
        "checked_at": "2026-01-01T00:00:00Z",
    }
    status.update(overrides)
    return status


STATUSES = {
    "configured": {"status": _status(), "meta": {"stale": False, "unavailable": False, "lastOkAt": None}},
    "stopped": {"status": _status(phase="stopped", workers_max=0, can_stop=False), "meta": {}},
    "unavailable": {
        "status": _status(runpod_error="timeout", workers_max=None, workers_min=None, live=UNAVAILABLE_LIVE),
        "meta": {},
    },
    "stale": {
        "status": _status(live=dict(IDLE_LIVE, label="Idle — no workers running")),
        "meta": {"stale": True, "unavailable": True, "lastOkAt": 1767225600000},
    },
}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BannerRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        cls._tmp = tmp
        statuses_path = Path(tmp.name) / "statuses.json"
        statuses_path.write_text(json.dumps(STATUSES), encoding="utf-8")
        proc = subprocess.run(
            ["node", "-e", HARNESS, str(DEPLOY_JS), str(statuses_path)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.html = json.loads(proc.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _visible(self, name):
        before = self.html[name].split('<details class="gpu-advanced deploy-advanced">', 1)[0]
        import re

        return re.sub(r'href="[^"]*"', 'href=""', before)

    def test_configured_state_and_facts_are_visible(self):
        visible = self._visible("configured")
        self.assertIn("Idle — no workers running", visible)
        self.assertIn("Zero workers is normal", visible)
        self.assertIn("RTX A5000", visible)
        self.assertIn("$0.270/hr per active worker", visible)
        self.assertIn("Scales to zero when idle", visible)
        self.assertIn("3 configured", visible)
        self.assertNotIn("active count not reported", visible)
        self.assertNotIn("Workers scaled to 0", visible)

    def test_identifiers_and_timings_only_inside_advanced(self):
        for name in ("configured", "stopped", "unavailable"):
            with self.subTest(status=name):
                visible = self._visible(name)
                for hidden in (ENDPOINT_ID, VOLUME_ID, "delayTime", "executionTime"):
                    self.assertNotIn(hidden, visible)
                advanced = self.html[name].split('<details class="gpu-advanced deploy-advanced">', 1)[1]
                self.assertIn(ENDPOINT_ID, advanced)

    def test_unavailable_is_never_shown_as_idle_or_running(self):
        unavailable = self._visible("unavailable")
        self.assertIn("Status unavailable", unavailable)
        self.assertNotIn("Idle — no workers running", unavailable)
        self.assertNotIn(">Running<", unavailable)

    def test_stale_result_is_marked_with_the_last_successful_time(self):
        stale = self._visible("stale")
        self.assertIn("Status unavailable", stale)
        self.assertIn("Showing the last successful check", stale)
        self.assertNotIn("Running ", stale.split("live-label", 1)[1].split("</p>", 1)[0])

    def test_stop_and_delete_buttons_are_inside_advanced(self):
        advanced_start = self.html["configured"].split('<details class="gpu-advanced deploy-advanced">', 1)
        self.assertNotIn('id="stopDeployPodBtn"', advanced_start[0])
        self.assertIn('id="stopDeployPodBtn"', advanced_start[1])
        self.assertIn('id="deleteNetworkVolumeBtn"', advanced_start[1])
        self.assertIn('id="refreshDeployStatusBtn"', advanced_start[0])


if __name__ == "__main__":
    unittest.main()
