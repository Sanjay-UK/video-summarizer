"""Processing mode lives on the GPU selection page, and infrastructure details are not shown to users.

The homepage must not offer a mode toggle or a GPU link. The GPU page shows its GPU options only in GPU mode, and the
local mode never submits them. Text that users see (progress lines, failure messages, status messages) must not name the
GPU provider, workers, endpoints or storage.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src.user_messages import (  # noqa: E402
    GENERIC_FAILURE,
    GENERIC_HIGHLIGHT_FAILURE,
    is_internal_detail,
    public_error,
    public_log_line,
)

TEMPLATES = ROOT / "templates"
APP_JS = ROOT / "static" / "js" / "app.js"


class PublicLogLineTests(unittest.TestCase):
    def test_infrastructure_lines_are_hidden(self):
        for line in [
            "RunPod GPU is transcribing and summarizing.",
            "RunPod SSH: starting heavy worker in /workspace/x",
            "Endpoint was paused (max workers 0); raised max workers to restore capacity.",
            "[capacity] background recovery for abc: OK",
            "RunPod job 9f8e7d: IN_QUEUE",
            "Heavy steps: RunPod GPU (Whisper CUDA, Ollama summarize)",
            "Uploading to the S3 bucket via a presigned link",
        ]:
            with self.subTest(line=line):
                self.assertIsNone(public_log_line(line))

    def test_step_lines_show_the_user_label(self):
        self.assertEqual(public_log_line("Step: highlight_assembly (running, no measurable progress)"),
                         "Step: Rendering highlight video (running, no measurable progress)")
        self.assertEqual(public_log_line("Step: transcribing (30%)"), "Step: Transcribing (30%)")

    def test_ordinary_progress_lines_pass_through(self):
        self.assertEqual(public_log_line("Audio generated with hierarchy prosody (900 chars)"),
                         "Audio generated with hierarchy prosody (900 chars)")

    def test_empty_lines_are_hidden(self):
        self.assertIsNone(public_log_line("   "))


class PublicErrorTests(unittest.TestCase):
    def test_provider_and_worker_errors_become_neutral(self):
        self.assertEqual(public_error("RunPod /run failed (HTTP 502): 502 Bad Gateway"), GENERIC_FAILURE)
        self.assertEqual(public_error("worker crashed on endpoint abc"), GENERIC_FAILURE)
        self.assertEqual(public_error("The highlight is missing from S3."), GENERIC_FAILURE)

    def test_actionable_messages_are_kept(self):
        message = "This YouTube video requires sign-in and could not be downloaded. Please upload the video file instead."
        self.assertEqual(public_error(message), message)

    def test_missing_message_uses_the_default(self):
        self.assertEqual(public_error(None), GENERIC_FAILURE)
        self.assertEqual(public_error("  ", default=GENERIC_HIGHLIGHT_FAILURE), GENERIC_HIGHLIGHT_FAILURE)

    def test_detection_is_case_insensitive(self):
        self.assertTrue(is_internal_detail("runpod endpoint"))
        self.assertTrue(is_internal_detail("Gpu worker"))
        self.assertFalse(is_internal_detail("Translation is included."))


class JobProgressUserTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:
            raise unittest.SkipTest("app.py is not part of the worker image")
        cls.app_module = app_module

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(self.app_module, "OUTPUT_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.job = self.app_module.JobProgress("a1b2c3d4", "upload")

    def test_progress_screen_never_receives_infrastructure_lines(self):
        with mock.patch("builtins.print"):
            self.job.add_log("RunPod SSH: starting heavy worker in /workspace/x")
            self.job.add_log("Audio generated with hierarchy prosody (900 chars)")
        lines = self.job.to_dict()["log_lines"]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("RunPod", " ".join(lines))

    def test_console_keeps_the_full_detail(self):
        with mock.patch("builtins.print") as printed:
            self.job.add_log("RunPod SSH: starting heavy worker in /workspace/x")
        printed.assert_called_once()
        self.assertIn("RunPod SSH", printed.call_args.args[0])

    def test_failure_message_shown_to_users_is_neutral_for_infrastructure_errors(self):
        self.job.mark_failed("RunPod /run failed (HTTP 500): internal")
        self.assertEqual(self.job.to_dict()["error"], GENERIC_FAILURE)

    def test_failure_message_for_users_is_kept_when_it_is_actionable(self):
        self.job.mark_failed("This YouTube video is private. Please upload the file instead.", code="YOUTUBE_PRIVATE")
        self.assertIn("private", self.job.to_dict()["error"])


class HomepageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:
            raise unittest.SkipTest("app.py is not part of the worker image")
        cls.client = app_module.app.test_client()

    def test_homepage_has_no_mode_toggle_or_gpu_link(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("pipelineRuntimeToggle", html)
        self.assertNotIn("navbarGpuLink", html)
        self.assertNotIn('href="/gpu-selection"', html)

    def test_homepage_shows_no_infrastructure_wording(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotRegex(html.lower(), r"runpod|\bgpu\b|serverless|endpoint")

    def test_summarize_form_no_longer_sends_a_mode_from_the_homepage(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertNotIn("formData.append('pipeline_runtime'", js)
        self.assertNotIn("(RunPod GPU)", js)
        self.assertNotIn("Translation uses Google", (TEMPLATES / "index.html").read_text(encoding="utf-8"))

    def test_gpu_selection_route_still_exists(self):
        self.assertEqual(self.client.get("/gpu-selection").status_code, 200)


class GpuModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:
            raise unittest.SkipTest("app.py is not part of the worker image")
        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    def _page(self, mode):
        with mock.patch.object(self.app_module, "load_pipeline_runtime", return_value=mode), \
                mock.patch.object(self.app_module, "get_compute_deploy_status",
                                  return_value={"has_deploy": False, "show_deploy_banner": False}):
            return self.client.get("/gpu-selection").get_data(as_text=True)

    def test_default_mode_is_local_and_hides_gpu_options(self):
        html = self._page("local")
        self.assertRegex(html, r'value="local" checked')
        self.assertIn('id="gpuOptions" class="gpu-options" hidden', html)
        self.assertIn('id="localModeNote"', html)
        self.assertNotRegex(html, r'id="localModeNote" hidden')

    def test_gpu_mode_shows_the_gpu_options(self):
        html = self._page("runpod")
        self.assertRegex(html, r'value="runpod" checked')
        self.assertIn('id="gpuOptions" class="gpu-options"', html)
        self.assertNotIn('id="gpuOptions" class="gpu-options" hidden', html)
        self.assertIn('id="localModeNote" hidden', html)

    def test_local_mode_disables_gpu_controls_in_the_script(self):
        html = self._page("local")
        self.assertIn("gpuOptions.querySelectorAll('input, select, textarea, button')", html)
        self.assertIn("el.disabled = !gpu", html)

    def test_mode_is_saved_through_the_existing_runtime_endpoint(self):
        html = self._page("local")
        self.assertIn("fetch('/api/pipeline-runtime'", html)
        self.assertIn("name=\"processing_mode\"", html)

    def test_gpu_page_no_longer_shows_provider_names_as_visible_text(self):
        for mode in ("local", "runpod"):
            with self.subTest(mode=mode):
                html = self._page(mode)
                visible = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
                visible = re.sub(r'href="[^"]*"', "", visible)
                self.assertNotIn("RunPod", visible)


class SavedModeDefaultTests(unittest.TestCase):
    def test_no_saved_selection_defaults_to_local(self):
        from src import compute_deploy, pipeline_runtime as pr
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(compute_deploy, "SELECTION_PATH", Path(tmp) / "missing.json"):
            self.assertEqual(pr.load_pipeline_runtime(), pr.RUNTIME_LOCAL)


if __name__ == "__main__":
    unittest.main()
