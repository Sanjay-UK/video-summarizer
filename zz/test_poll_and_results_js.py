"""Browser-side rules for progress polling, subtask checkmarks, stage labels, and results loading.

Node loads the real functions from static/js/app.js. Results loading is tested with a stubbed fetch, so the status
handling (422, 500, 404, network error, success) is checked without a browser. Skipped without Node.
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
function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0) throw new Error('missing section: ' + from);
  return src.slice(a, b);
}
const subtaskBlock = src.slice(src.indexOf('// Pipeline order of the steps'), src.indexOf('let highlightRequested'));
const API_BASE = '/api';
const failureRule = new Function(slice('function failureMessageFor(', 'let progressGeneration = 0;') + '; return { failureMessageFor };')();
const pollRules = new Function('API_BASE',
  slice('function shouldApplyPoll(', 'function startProgressPolling(') + '; return { shouldApplyPoll };')(API_BASE);
const views = new Function('API_BASE',
  slice('const STEP_LABELS = {', 'function updateProgressUI(') + '; return { progressViewFor, stageLabel };')(API_BASE);
// A fresh copy per checklist, because the furthest-step memory is per job.
const makeChecklist = () => new Function(subtaskBlock + '\n' +
  slice('function subtaskStates(', 'async function fetchResultsOrError(') + '; return { subtaskStates, updateSubtaskUI };')();
function fakeElement() {
  const classes = new Set();
  return {
    classes,
    style: {},
    classList: { toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); } },
    querySelector: () => ({ textContent: '' }),
  };
}
const subtasks = makeChecklist();
const results = new Function('API_BASE',
  slice('async function fetchResultsOrError(', 'async function showResults(') + '; return { fetchResultsOrError };')(API_BASE);
const fetchStub = (behaviour) => (url, opts) => behaviour(url, opts);
(async () => {
  const out = {};
  const current = { jobId: 'a1b2c3d4', generation: 2, appliedSequence: 5 };
  const ok = (over) => Object.assign({ jobId: 'a1b2c3d4', generation: 2, sequence: 6, responseJobId: 'a1b2c3d4' }, over);
  out.poll = {
    inOrder: pollRules.shouldApplyPoll(current, ok({})),
    switchedJob: pollRules.shouldApplyPoll(current, ok({ jobId: 'ffffffff' })),
    previousGeneration: pollRules.shouldApplyPoll(current, ok({ generation: 1 })),
    outOfOrder: pollRules.shouldApplyPoll(current, ok({ sequence: 4 })),
    sameAsApplied: pollRules.shouldApplyPoll(current, ok({ sequence: 5 })),
    responseForOtherJob: pollRules.shouldApplyPoll(current, ok({ responseJobId: 'ffffffff' })),
    responseWithoutJobId: pollRules.shouldApplyPoll(current, ok({ responseJobId: null })),
  };
  // Checkmarks and bar use the same displayed value.
  const view = views.progressViewFor({ status: 'processing', progress: 0.2 }, 50);
  out.consistency = {
    barPercent: view.percent,
    checkmarksAt: subtasks.subtaskStates('transcribing', false),
  };
  // Checklist driven through updateSubtaskUI against a stub DOM, as the progress poll does it.
  const snapshot = (dom) => Object.fromEntries(Object.entries(dom).map(([id, el]) => [id.replace('subtask-', ''), {
    state: el.classes.has('active') ? 'active' : el.classes.has('completed') ? 'completed' : 'pending',
    display: el.style.display || '',
  }]));
  const stubDocument = () => {
    const dom = {};
    globalThis.document = { getElementById: (id) => dom[id] || (dom[id] = fakeElement()) };
    return dom;
  };
  const highlightRun = stubDocument();
  const highlightChecklist = makeChecklist();
  highlightChecklist.updateSubtaskUI('highlight_assembly', false);
  out.checklist = { highlightAssembly: snapshot(highlightRun) };
  highlightChecklist.updateSubtaskUI('generating_subtitles', false);
  out.checklist.afterStaleSubtitlesReport = snapshot(highlightRun);
  const summarizeRun = stubDocument();
  makeChecklist().updateSubtaskUI('translating', false);
  out.checklist.translating = snapshot(summarizeRun);
  const doneRun = stubDocument();
  makeChecklist().updateSubtaskUI('highlight_complete', true);
  out.checklist.finished = snapshot(doneRun);
  out.labels = {
    translating: views.progressViewFor({ status: 'processing', indeterminate: true, current_step: 'translating' }, 60).label,
    highlight: views.progressViewFor({ status: 'processing', indeterminate: true, current_step: 'highlight_assembly' }, 92).label,
    unknown: views.stageLabel('something_new'),
  };
  out.indeterminateKeepsNumber = views.progressViewFor({ status: 'processing', indeterminate: true, current_step: 'highlight_assembly' }, 92).percent;

  const respond = (status, body, isOk) => async () => ({ ok: isOk, status, json: async () => body });
  const run = async (behaviour) => {
    globalThis.fetch = fetchStub(behaviour);
    return results.fetchResultsOrError('a1b2c3d4');
  };
  out.results = {
    success: await run(respond(200, { status: 'completed', summary: 'x' }, true)),
    unprocessable: await run(respond(422, { error: 'This YouTube video requires sign-in.', code: 'YOUTUBE_AUTH_REQUIRED' }, false)),
    serverError: await run(respond(500, { error: 'boom' }, false)),
    notFound: await run(respond(404, { error: 'Job not found' }, false)),
    notFoundNoBody: await run(async () => ({ ok: false, status: 404, json: async () => { throw new Error('no json'); } })),
    network: await run(async () => { throw new TypeError('Failed to fetch'); }),
    badSuccessBody: await run(respond(200, 'not an object', true)),
  };
  out.failure = {
    fromPayload: failureRule.failureMessageFor({ status: 'error', error: 'FFmpeg extraction failed' }),
    missing: failureRule.failureMessageFor({ status: 'error' }),
    blank: failureRule.failureMessageFor({ status: 'error', error: '   ' }),
    notText: failureRule.failureMessageFor({ status: 'error', error: 42 }),
  };
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PollAndResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["node", "-e", HARNESS, str(APP_JS)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_poll_for_the_current_job_in_order_is_applied(self):
        self.assertTrue(self.r["poll"]["inOrder"])

    def test_switching_jobs_discards_the_response(self):
        self.assertFalse(self.r["poll"]["switchedJob"])

    def test_a_response_from_a_previous_polling_run_is_discarded(self):
        self.assertFalse(self.r["poll"]["previousGeneration"])

    def test_out_of_order_and_repeated_responses_are_discarded(self):
        self.assertFalse(self.r["poll"]["outOfOrder"])
        self.assertFalse(self.r["poll"]["sameAsApplied"])

    def test_a_response_claiming_another_job_is_discarded(self):
        self.assertFalse(self.r["poll"]["responseForOtherJob"])

    def test_responses_without_a_job_id_are_still_accepted(self):
        self.assertTrue(self.r["poll"]["responseWithoutJobId"], "older servers do not send job_id")

    def test_checkmarks_follow_the_reported_step_in_pipeline_order(self):
        self.assertEqual(self.r["consistency"]["barPercent"], 50)
        states = self.r["consistency"]["checkmarksAt"]
        self.assertEqual(states["extracting_audio"], "completed")
        self.assertEqual(states["transcribing"], "active")
        self.assertEqual(states["summarizing"], "pending")

    def test_status_at_highlight_rendering_shows_every_earlier_step_done(self):
        # Regression: the status line said "Rendering highlight video (RunPod GPU)" while the checklist stopped at
        # Summarize, because the checklist compared fixed percentages with the backend's values.
        items = self.r["checklist"]["highlightAssembly"]
        for name in ("download", "audio", "transcribe", "summarize", "audio-gen", "subtitles"):
            self.assertEqual(items[name]["state"], "completed", name)
        self.assertEqual(items["highlight"]["state"], "active")
        self.assertNotEqual(items["highlight"]["display"], "none")

    def test_a_late_earlier_step_report_does_not_move_the_checklist_back(self):
        items = self.r["checklist"]["afterStaleSubtitlesReport"]
        self.assertEqual(items["highlight"]["state"], "active")
        self.assertEqual(items["subtitles"]["state"], "completed")

    def test_translating_is_part_of_the_summarize_item(self):
        items = self.r["checklist"]["translating"]
        self.assertEqual(items["summarize"]["state"], "active")
        self.assertEqual(items["transcribe"]["state"], "completed")
        self.assertEqual(items["audio-gen"]["state"], "pending")

    def test_completed_job_marks_every_item_done(self):
        items = self.r["checklist"]["finished"]
        for name, item in items.items():
            self.assertEqual(item["state"], "completed", name)

    def test_stage_labels_name_the_real_stage(self):
        self.assertEqual(self.r["labels"]["translating"], "Translating summary")
        self.assertEqual(self.r["labels"]["highlight"], "Rendering highlight video")
        self.assertEqual(self.r["labels"]["unknown"], "Working")

    def test_indeterminate_stage_keeps_the_last_confirmed_number(self):
        self.assertEqual(self.r["indeterminateKeepsNumber"], 92)

    def test_success_returns_the_results(self):
        self.assertTrue(self.r["results"]["success"]["ok"])
        self.assertEqual(self.r["results"]["success"]["results"]["status"], "completed")

    def test_failed_job_422_shows_its_own_actionable_message(self):
        outcome = self.r["results"]["unprocessable"]
        self.assertFalse(outcome["ok"])
        self.assertIn("requires sign-in", outcome["error"])
        self.assertEqual(outcome["code"], "YOUTUBE_AUTH_REQUIRED")

    def test_failed_job_500_shows_its_error(self):
        self.assertEqual(self.r["results"]["serverError"]["error"], "boom")

    def test_missing_job_and_unreadable_error_bodies_get_clear_messages(self):
        self.assertEqual(self.r["results"]["notFound"]["error"], "Job not found")
        self.assertEqual(self.r["results"]["notFoundNoBody"]["error"], "This job was not found.")

    def test_network_failure_is_reported_not_thrown(self):
        self.assertFalse(self.r["results"]["network"]["ok"])
        self.assertIn("Could not reach the server", self.r["results"]["network"]["error"])

    def test_live_failure_shows_the_job_message(self):
        self.assertEqual(self.r["failure"]["fromPayload"], "FFmpeg extraction failed")

    def test_missing_or_blank_failure_message_falls_back_to_generic_text(self):
        self.assertEqual(self.r["failure"]["missing"], "An error occurred during processing")
        self.assertEqual(self.r["failure"]["blank"], "An error occurred during processing")
        self.assertEqual(self.r["failure"]["notText"], "An error occurred during processing")

    def test_a_non_object_success_body_is_not_treated_as_results(self):
        self.assertFalse(self.r["results"]["badSuccessBody"]["ok"])


if __name__ == "__main__":
    unittest.main()
