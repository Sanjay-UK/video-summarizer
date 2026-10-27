"""Timing and safety rules of the status poller in static/js/deploy_status.js.

Node runs the real createStatusPoller with a fake clock, fake visibility, and fake requests that the test resolves
or rejects by hand. The harness reports what happened; the Python tests assert on it. Skipped without Node.
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
vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const UI = globalThis.DeployStatusUI;

function scenario() {
  const log = { fetches: [], renders: [], timers: 0 };
  let now = 1000000;
  let nextId = 1;
  let timers = [];
  let visible = true;
  const pending = [];
  const clock = {
    setTimeout(fn, ms) { const id = nextId++; timers.push({ id, at: now + ms, fn }); return id; },
    clearTimeout(id) { timers = timers.filter(t => t.id !== id); },
  };
  function advance(ms) {
    const end = now + ms;
    for (;;) {
      timers.sort((a, b) => a.at - b.at);
      const next = timers[0];
      if (!next || next.at > end) break;
      timers.shift();
      now = next.at;
      next.fn();
    }
    now = end;
  }
  function fetch(url, opts) {
    log.fetches.push({ url, method: (opts && opts.method) || 'GET', at: now, signal: opts && opts.signal });
    return new Promise((resolve, reject) => {
      const entry = { resolve, reject, signal: opts && opts.signal };
      pending.push(entry);
      if (opts && opts.signal) opts.signal.addEventListener('abort', () => reject(new Error('AbortError')));
    });
  }
  function makeController() {
    const listeners = [];
    const signal = { aborted: false, addEventListener(t, f) { listeners.push(f); } };
    return { signal, abort() { signal.aborted = true; listeners.forEach(f => f()); } };
  }
  const poller = UI.createStatusPoller({
    fetch, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    now: () => now, isVisible: () => visible, createAbortController: makeController,
    render: (status, meta) => log.renders.push({ status, meta: Object.assign({}, meta), at: now }),
  });
  const ok = (body) => ({ ok: true, status: 200, json: () => Promise.resolve(body) });
  const settleNext = async (fn) => { const e = pending.shift(); await flush(); fn(e); await flush(); };
  const flush = () => new Promise(r => setTimeout(r, 0));
  function runNextTimer() { timers.sort((a, b) => a.at - b.at); const t = timers.shift(); if (!t) return; now = t.at; t.fn(); }
  return { log, poller, advance, runNextTimer, settleNext, ok, pending, setVisible(v) { visible = v; }, timers: () => timers.length, now: () => now, flush };
}

(async () => {
  const out = {};
  // 1. immediate fetch, then 5 s interval while visible
  let s = scenario();
  s.poller.start();
  out.immediate = s.log.fetches.length;
  await s.settleNext(e => e.resolve(s.ok({ show_deploy_banner: false })));
  s.advance(4999); out.beforeInterval = s.log.fetches.length;
  s.advance(1); out.atInterval = s.log.fetches.length;
  out.method = s.log.fetches[0].method; out.url = s.log.fetches[0].url;
  s.advance(15000); out.allRequests = s.log.fetches.map(f => f.method + ' ' + f.url);

  // 2. hidden: no timers; resume refreshes immediately
  s = scenario();
  s.poller.start();
  await s.settleNext(e => e.resolve(s.ok({})));
  s.setVisible(false); s.poller.pause(); s.advance(60000); out.hiddenFetches = s.log.fetches.length;
  s.setVisible(true); s.poller.resume(); out.resumedFetches = s.log.fetches.length;
  await s.settleNext(e => e.resolve(s.ok({})));

  // 3. no overlap: manual refresh while in flight reuses the request
  s = scenario();
  const first = s.poller.start();
  const again = s.poller.refreshNow();
  out.sameInFlight = first === again;
  out.overlapFetches = s.log.fetches.length;
  await s.settleNext(e => e.resolve(s.ok({})));

  // 4. hidden while a request is in flight: no new timer after it completes
  s = scenario();
  s.poller.start();
  s.setVisible(false); s.poller.pause();
  await s.settleNext(e => e.resolve(s.ok({})));
  s.advance(60000); out.hiddenInFlightFetches = s.log.fetches.length;

  // 5. bounded backoff on failures, then stale result retained and marked
  s = scenario();
  s.poller.start();
  await s.settleNext(e => e.resolve(s.ok({ show_deploy_banner: true, phase: 'ready' })));
  s.runNextTimer(); await s.flush();          // the 5 s interval fires; a request is now pending
  const fetchTimes = [s.log.fetches[s.log.fetches.length - 1].at];
  for (let i = 0; i < 4; i++) {
    const e = s.pending.shift();
    e.reject(new Error('network')); await s.flush();   // this request fails
    s.runNextTimer(); await s.flush();                  // the backoff timer fires; the next request starts
    fetchTimes.push(s.log.fetches[s.log.fetches.length - 1].at);
  }
  out.backoffGaps = [];
  for (let i = 1; i < fetchTimes.length; i++) out.backoffGaps.push(fetchTimes[i] - fetchTimes[i - 1]);
  const lastRender = s.log.renders[s.log.renders.length - 1];
  out.stale = { stale: lastRender.meta.stale, unavailable: lastRender.meta.unavailable, hasLast: !!lastRender.status, lastOkAt: lastRender.meta.lastOkAt };
  out.renderStatus = lastRender.status && lastRender.status.phase;

  // 6. recovery resets the delay; the next interval is 5 s again
  s = scenario();
  s.poller.start();
  await s.settleNext(e => e.reject(new Error('network')));
  s.advance(10000);
  await s.settleNext(e => e.resolve(s.ok({})));
  const afterRecovery = s.log.fetches.length;
  s.advance(5000);
  out.recoveryFetchGap = s.log.fetches.length - afterRecovery;
  out.recoveryMeta = s.log.renders[s.log.renders.length - 1].meta;

  // 7. timeout aborts a request that never answers
  s = scenario();
  s.poller.start();
  s.advance(25000);
  out.timedOutAborted = !!(s.log.fetches[0].signal && s.log.fetches[0].signal.aborted);
  await s.flush();
  out.timeoutRenderedUnavailable = s.log.renders.length > 0 && s.log.renders[0].meta.unavailable === true;

  // 8. stop cancels the request and every timer; no further fetches
  s = scenario();
  s.poller.start();
  s.poller.stop();
  out.stopAborted = !!(s.log.fetches[0].signal && s.log.fetches[0].signal.aborted);
  out.timersAfterStop = s.timers();
  s.advance(120000);
  out.fetchesAfterStop = s.log.fetches.length;

  // 9. every request is a read-only GET to the status endpoint
  out.allGet = [];
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class StatusPollerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ["node", "-e", HARNESS, str(DEPLOY_JS)], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_fetches_immediately_and_every_five_seconds_while_visible(self):
        self.assertEqual(self.r["immediate"], 1)
        self.assertEqual(self.r["beforeInterval"], 1, "no request before the 5 s interval")
        self.assertEqual(self.r["atInterval"], 2)

    def test_only_reads_the_status_endpoint_with_get(self):
        self.assertEqual(self.r["method"], "GET")
        self.assertEqual(self.r["url"], "/api/compute-deploy-status")
        self.assertTrue(self.r["allRequests"])
        self.assertEqual(set(self.r["allRequests"]), {"GET /api/compute-deploy-status"},
                         "polling must never send anything but a read-only GET")

    def test_pauses_while_hidden_and_refreshes_immediately_on_return(self):
        self.assertEqual(self.r["hiddenFetches"], 1, "no polling while hidden")
        self.assertEqual(self.r["resumedFetches"], 2, "one immediate refresh when visible again")

    def test_no_overlapping_requests(self):
        self.assertTrue(self.r["sameInFlight"], "a refresh during a request reuses it")
        self.assertEqual(self.r["overlapFetches"], 1)

    def test_no_new_request_when_hidden_during_an_in_flight_request(self):
        self.assertEqual(self.r["hiddenInFlightFetches"], 1)

    def test_backoff_doubles_and_is_capped(self):
        self.assertEqual(self.r["backoffGaps"], [10000, 20000, 40000, 60000])

    def test_failure_keeps_last_result_marked_stale_with_its_time(self):
        self.assertTrue(self.r["stale"]["stale"])
        self.assertTrue(self.r["stale"]["unavailable"])
        self.assertTrue(self.r["stale"]["hasLast"])
        self.assertIsNotNone(self.r["stale"]["lastOkAt"])
        self.assertEqual(self.r["renderStatus"], "ready")

    def test_recovery_clears_stale_and_returns_to_five_second_interval(self):
        self.assertEqual(self.r["recoveryFetchGap"], 1)
        self.assertFalse(self.r["recoveryMeta"]["stale"])
        self.assertFalse(self.r["recoveryMeta"]["unavailable"])

    def test_timeout_aborts_and_reports_unavailable(self):
        self.assertTrue(self.r["timedOutAborted"])
        self.assertTrue(self.r["timeoutRenderedUnavailable"])

    def test_stop_cancels_requests_and_timers(self):
        self.assertTrue(self.r["stopAborted"])
        self.assertEqual(self.r["timersAfterStop"], 0)
        self.assertEqual(self.r["fetchesAfterStop"], 1)


if __name__ == "__main__":
    unittest.main()
