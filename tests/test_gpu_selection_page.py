"""The simplified /gpu-selection page: what the main view shows, what stays in Advanced, and the form contract.

The page is rendered through Flask's test client with a fixed report and deployment status, so no RunPod call is
made. Checks look at the rendered HTML: visible text outside the 'Advanced details' sections must be free of
endpoint, volume, pod, and timing details, and the form fields the save script reads must still exist.
"""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

ENDPOINT_ID = "EP-TEST-ENDPOINT-123"
VOLUME_ID = "VOL-TEST-VOLUME-456"
ADVANCED_MARK = '<details class="gpu-advanced deploy-advanced">'


def _row(selection_id="sl-a5000", name="RTX A5000", price=0.27):
    return {
        "selection_id": selection_id,
        "name": name,
        "data_center_id": "AP-IN-1",
        "data_center_name": "India",
        "serverless_pool": "AMPERE_24",
        "vram_gb": 24,
        "price_per_hour": price,
        "availability": "High",
        "cloud_badges": "Secure",
        "notes": "",
        "kind": "serverless",
    }


def _serverless_report(**overrides):
    from src.runpod_platform_controls import platform_controls_status
    from src.serverless_model_catalog import catalog_for_ui, DEFAULT_SERVERLESS_MODELS

    row = _row()
    report = {
        "errors": [],
        "compute_mode": "serverless",
        "serverless_models_saved": dict(DEFAULT_SERVERLESS_MODELS, worker_image_source="custom_build", registry_image=""),
        "serverless_model_catalog": catalog_for_ui(),
        "serverless_image_configured": False,
        "serverless_image_value": None,
        "serverless_datacenters": [{"data_center_id": "AP-IN-1", "data_center_name": "India"}],
        "serverless_selected_data_center_id": "AP-IN-1",
        "serverless_saved_display": {"kind": "serverless", "offer": row, "saved_at": "2026-01-01T00:00:00Z"},
        "serverless_sections": [{"key": "ampere", "title": "Serverless GPUs", "rows": [row]}],
        "serverless_default_selection_id": row["selection_id"],
        "serverless_gpu_rows": [row],
        "recommendations": {
            "categories": [{"id": "full_pipeline", "title": "Full pipeline", "description": "Fits the whole pipeline.", "pick": row}],
            "recommended_gpu_for_pipeline": row,
        },
        "runpod_platform": platform_controls_status(),
        "saved_selection": None,
        "saved_selection_id": None,
        "deploy_status": None,
        "gpus_by_cloud": {},
        "cpus_by_cloud": {},
        "docker_hub_user": None,
        "fetched_at": "2026-01-01T00:00:00Z",
    }
    report.update(overrides)
    return report


def _live(max_workers=3, idle=0, running=0, in_queue=0, error=None):
    """Live state built by the real mapper from provider-shaped inputs (same path the server uses)."""
    from src.serverless_live_status import map_live_status

    config = {"workers": {"min": 0, "max": max_workers, "idleTimeout": 5}}
    health = {"jobs": {"inQueue": in_queue, "inProgress": 0}, "workers": {"idle": idle, "running": running}}
    return map_live_status(
        endpoint_configured=True,
        config=None if error else config,
        config_error=error,
        health=None if error else health,
        health_error=None,
        checked_at="2026-01-01T00:00:00Z",
    )


def _deployed_status(**overrides):
    status = {
        "live": _live(),
        "show_deploy_banner": True,
        "phase": "ready",
        "phase_label": "Serverless (scale to zero)",
        "compute_mode": "serverless",
        "hardware": {"name": "RTX A5000", "vram_gb": 24, "price_per_hour": 0.27},
        "endpoint_id": ENDPOINT_ID,
        "network_volume_id": VOLUME_ID,
        "pod_id": None,
        "workers_min": 0,
        "workers_max": 3,
        "serverless_idle_timeout_sec": 5,
        "serverless_max_cost_per_hour_usd": 0.81,
        "last_delay_time_ms": 64455,
        "last_execution_time_ms": 73012,
        "can_stop": True,
        "can_delete_volume": True,
        "stop_action_label": "Stop serverless",
        "console_url": f"https://www.runpod.io/console/serverless/user/endpoint/{ENDPOINT_ID}",
        "data_center_id": "AP-IN-1",
        "runpod_error": None,
        "runpod_live_status": None,
        "deploy_message": "Endpoint ready.",
        "warmup_status": "completed",
        "checked_at": "2026-01-01T00:00:00Z",
        "model_status": None,
        "idle_auto_stop_note": None,
        "spend_cap_note": None,
        "ssh_command": None,
        "billing_docs_url": None,
    }
    status.update(overrides)
    return status


def _visible_main(html: str) -> str:
    """The HTML before the Advanced details of the deployment card, with link targets removed.

    A console link necessarily contains the endpoint ID in its URL. Link targets are not shown as text, so they
    are removed before checking what the user can read.
    """
    before = html.split(ADVANCED_MARK, 1)[0]
    return re.sub(r'href="[^"]*"', 'href=""', before)


class GpuPageRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except ImportError:
            raise unittest.SkipTest("app.py is not part of the worker image")
        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    def _render(self, report, status):
        with mock.patch.object(self.app_module, "build_gpu_selection_report", return_value=report), \
                mock.patch.object(self.app_module, "get_compute_deploy_status", return_value=status):
            response = self.client.get("/gpu-selection")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_main_view_hides_endpoint_volume_and_timing_details(self):
        html = self._render(_serverless_report(), _deployed_status())
        visible = _visible_main(html)
        for secret_like in (ENDPOINT_ID, VOLUME_ID, "delayTime", "executionTime", "Idle timeout", "Pod ID"):
            self.assertNotIn(secret_like, visible, f"{secret_like!r} must be under Advanced details")
        self.assertIn(ENDPOINT_ID, html.split(ADVANCED_MARK, 1)[1], "the identifiers stay available in Advanced")
        self.assertIn("Last delayTime", html)

    def test_main_view_shows_state_gpu_region_rate_scaling_and_worker_limit(self):
        html = self._render(_serverless_report(), _deployed_status())
        visible = _visible_main(html)
        self.assertIn("Idle — no workers running", visible)
        self.assertIn("RTX A5000", visible)
        self.assertIn("AP-IN-1", visible)
        self.assertIn("$0.270/hr per active worker", visible)
        self.assertIn("Scales to zero when idle", visible)
        self.assertIn("3 configured", visible)
        self.assertIn("Reported: running 0 · idle 0", visible)
        self.assertNotIn("active count not reported", visible)
        self.assertIn('id="refreshDeployStatusBtn"', visible)
        self.assertIn("Open RunPod", visible)

    def test_unverified_status_is_not_presented_as_running(self):
        status = _deployed_status(runpod_error="timeout talking to RunPod", workers_max=None, workers_min=None,
                                  live=_live(error="timeout talking to RunPod"))
        visible = _visible_main(self._render(_serverless_report(), status))
        self.assertIn("Status unavailable", visible)
        self.assertIn("Could not read the endpoint settings", visible)
        self.assertNotIn("Idle — no workers running", visible)
        self.assertNotIn(">Running<", visible)

    def test_scaled_to_zero_state_is_labelled(self):
        status = _deployed_status(phase="stopped", workers_max=0, can_stop=False, live=_live(max_workers=0))
        visible = _visible_main(self._render(_serverless_report(), status))
        self.assertIn("Idle — no workers running", visible)
        self.assertIn("Zero workers is normal", visible)
        self.assertNotIn("Workers scaled to 0", visible)

    def test_stop_and_delete_are_only_in_advanced_details(self):
        html = self._render(_serverless_report(), _deployed_status())
        advanced = html.split(ADVANCED_MARK, 1)[1]
        self.assertIn('id="stopDeployPodBtn"', advanced)
        self.assertIn('id="deleteNetworkVolumeBtn"', advanced)
        self.assertNotIn('id="stopDeployPodBtn"', html.split(ADVANCED_MARK, 1)[0])
        self.assertNotIn('id="deleteNetworkVolumeBtn"', html.split(ADVANCED_MARK, 1)[0])

    def test_no_deployment_shows_a_plain_message(self):
        html = self._render(_serverless_report(), {"show_deploy_banner": False})
        self.assertIn("No endpoint has been deployed yet", html)

    def test_models_are_visible_and_advanced_fields_are_not_required(self):
        html = self._render(_serverless_report(), _deployed_status())
        main_part = html.split("<summary>Advanced settings</summary>", 1)[0]
        self.assertIn('name="ss_whisper"', main_part)
        self.assertIn('name="ss_ollama"', main_part)
        for label in ("Whisper tiny", "Whisper base", "Whisper large-v3"):
            self.assertIn(label, main_part)
        advanced = html.split("<summary>Advanced settings</summary>", 1)[1]
        for name in ("ss_nllb", "ss_worker_image_source"):
            tags = re.findall(rf"<input[^>]*name=\"{name}\"[^>]*>", advanced)
            self.assertTrue(tags, name)
            for tag in tags:
                self.assertNotIn("required", tag, f"{name} is inside a collapsed section and must not block submit")
            self.assertTrue(any("checked" in tag for tag in tags), f"{name} needs a default so the form always submits")

    def test_form_contract_fields_and_save_button_are_present(self):
        html = self._render(_serverless_report(), _deployed_status())
        self.assertEqual(html.count('id="saveDeployBtn"'), 1)
        self.assertIn("Save selection &amp; deploy", html)
        self.assertIn("can incur RunPod charges", html)
        for field in ('id="serverlessRegionSelect"', 'name="serverless_data_center_id"', 'id="ssRegistryImage"',
                      'name="docker_hub_user"', 'id="deployMessage"', 'id="computeSelectForm"'):
            self.assertIn(field, html)

    def test_recommendation_is_one_pick_without_category_table(self):
        html = self._render(_serverless_report(), _deployed_status())
        self.assertIn("Recommended: RTX A5000", html)
        self.assertIn("Fits the whole pipeline.", html)
        self.assertIn('data-selection-id="sl-a5000"', html)
        self.assertNotIn("Recommendations by category", html)
        self.assertNotIn("Suggested hardware", html)

    def test_catalog_drops_pool_and_keeps_selection_contract(self):
        html = self._render(_serverless_report(), _deployed_status())
        self.assertIn('data-serverless-dc="AP-IN-1"', html)
        self.assertIn('name="selection_id" value="sl-a5000"', html)
        self.assertNotIn(">Pool<", html)
        self.assertNotIn("AMPERE_24", _visible_main(html))

    def test_cost_copy_is_brief_and_states_no_app_spend_cap(self):
        html = self._render(_serverless_report(), _deployed_status())
        self.assertIn("does not enforce an account-wide spend cap", html)
        self.assertIn("scale to zero when idle", html)
        self.assertIn("docs.runpod.io", html)

    def test_pod_mode_keeps_its_existing_layout(self):
        row = _row(selection_id="pod-1", name="RTX 4090 Pod")
        report = _serverless_report(
            compute_mode="pod",
            serverless_model_catalog=None,
            gpus_by_cloud={"runpod-secure": [dict(row, cloud="RunPod Secure")]},
            cpus_by_cloud={},
        )
        html = self._render(report, {"show_deploy_banner": False})
        self.assertIn("Cloud compute selection", html)
        self.assertIn("RunPod GPUs (pick one)", html)
        self.assertIn("RTX 4090 Pod", html)
        self.assertIn("Available CPUs", html)


if __name__ == "__main__":
    unittest.main()
