"""Regression tests: a save that reuses an existing endpoint never starts a warmup job, and the GPU status
shows the saved endpoint ID. RunPod is faked; the save writes only to a temporary selection file.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import compute_deploy, compute_status, runpod_serverless  # noqa: E402

IMAGE = "sanjayuk80603/video-summarizer-worker:v1.0.9"
NAME = "video-summarizer-RTX A5000"
OFFER = {
    "provider": "runpod",
    "name": "RTX A5000",
    "data_center_id": "CA-MTL-1",
    "serverless_pool": "AMPERE_24",
    "cloud": "serverless",
}
MODELS = {"whisper": "base", "ollama": "llama3.2:3b", "nllb": "facebook/nllb-200-distilled-600M"}


def _read_only_api(endpoints):
    """RunPod stand-in. Reads are allowed. The only write allowed is a capacity-only PATCH (workers), which the
    lifecycle code sends to lower max workers. Anything else fails loudly, including any create or warmup."""

    def handler(method, path, *, body=None, timeout=120):
        if method == "GET" and path == "/serverless":
            return 200, {"endpoints": endpoints, "pagination": {}}
        if method == "GET" and path.startswith("/serverless/"):
            eid = path.rsplit("/", 1)[-1]
            for e in endpoints:
                if e["id"] == eid:
                    return 200, e
            return 404, {"detail": "not found"}
        if method == "PATCH" and path.startswith("/serverless/") and set(body or {}) == {"workers"}:
            eid = path.rsplit("/", 1)[-1]
            for e in endpoints:
                if e["id"] == eid:
                    e["workers"] = dict(body["workers"])
                    return 200, e
            return 404, {"detail": "not found"}
        raise AssertionError(f"state-changing RunPod call blocked: {method} {path}")

    return handler


class WarmupRequiredTests(unittest.TestCase):
    def test_new_endpoint_with_models_needs_warmup(self):
        deploy = {"status": "deployed", "endpoint_id": "ep1", "reused": False}
        self.assertTrue(compute_deploy.warmup_required(deploy, MODELS))

    def test_reused_endpoint_never_needs_warmup(self):
        deploy = {"status": "deployed", "endpoint_id": "ep1", "reused": True}
        self.assertFalse(compute_deploy.warmup_required(deploy, MODELS))

    def test_failed_or_missing_endpoint_or_models_skip_warmup(self):
        self.assertFalse(compute_deploy.warmup_required({"status": "failed", "endpoint_id": "ep1"}, MODELS))
        self.assertFalse(compute_deploy.warmup_required({"status": "deployed", "endpoint_id": ""}, MODELS))
        self.assertFalse(compute_deploy.warmup_required({"status": "deployed", "endpoint_id": "ep1"}, None))


class SaveReusesWithoutWarmupTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        # Lease files go to a temporary folder, never the project's data/ folder.
        import tempfile

        lease_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(lease_tmp.cleanup)
        lease_patch = mock.patch("src.endpoint_capacity.LEASE_DIR", Path(lease_tmp.name))
        lease_patch.start()
        self.addCleanup(lease_patch.stop)
        os.environ.update({
            "RUNPOD_SERVERLESS_IMAGE": IMAGE,
            "RUNPOD_NETWORK_VOLUME_ID": "vol1",
            "RUNPOD_API_KEY": "test-only-not-a-secret",
        })
        os.environ.pop("RUNPOD_SERVERLESS_NAME", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.selection = Path(self.tmp.name) / "compute_selection.json"
        self.selection.write_text(json.dumps({
            "serverless_endpoint_id": "ep1",
            "deploy": {"status": "failed", "endpoint_id": ""},
            "compute_mode": "serverless",
            "pipeline_runtime": "runpod",
        }), encoding="utf-8")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_save_reuses_endpoint_and_skips_warmup(self):
        endpoints = [{"id": "ep1", "name": NAME, "image": IMAGE, "requestUrls": {},
                      "workers": {"min": 0, "max": 3, "idleTimeout": 5}}]
        warmup = mock.Mock(side_effect=AssertionError("warmup must not run for a reused endpoint"))
        with mock.patch.object(compute_deploy, "SELECTION_PATH", self.selection), mock.patch.object(
            compute_deploy, "_resolve_offer", return_value=(dict(OFFER), True)
        ), mock.patch.object(compute_deploy, "load_compute_mode", return_value="serverless"), mock.patch(
            "src.serverless_worker_image_publish.sync_serverless_env_from_selection", return_value=None
        ), mock.patch(
            "src.runpod_serverless.serverless_image_registry_ready", return_value=True
        ), mock.patch.object(
            runpod_serverless, "_request", side_effect=_read_only_api(endpoints)
        ), mock.patch.object(
            runpod_serverless, "run_warmup_cold_start", warmup
        ), mock.patch(
            "src.runpod_serverless.save_job_metrics_to_selection", return_value=None
        ):
            result = compute_deploy.save_selection_and_deploy(
                "gpu:runpod:runpod-serverless:CA-MTL-1|AMPERE_24|NVIDIA RTX A5000",
                client_offer=dict(OFFER),
                serverless_models=dict(MODELS),
                serverless_data_center_id="CA-MTL-1",
            )
        warmup.assert_not_called()
        self.assertTrue(result["ok"])
        deploy = json.loads(self.selection.read_text(encoding="utf-8"))["deploy"]
        self.assertEqual(deploy["endpoint_id"], "ep1")
        self.assertEqual(deploy["warmup_status"], "skipped_reused")
        self.assertIn("not recreated", deploy["message"])


class StatusShowsSavedEndpointTests(unittest.TestCase):
    def _status_for(self, saved):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "compute_selection.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        with mock.patch.object(compute_status, "SELECTION_PATH", path), mock.patch.object(
            compute_status, "get_pipeline_model_status", return_value={}
        ), mock.patch.object(compute_status, "platform_controls_status", return_value={}):
            return compute_status.get_compute_deploy_status(refresh_live=False)

    def _base(self, **overrides):
        base = {
            "offer": dict(OFFER),
            "compute_mode": "serverless",
            "serverless_models": dict(MODELS),
            "deploy": {"status": "failed", "endpoint_id": "", "message": "old failure"},
        }
        base.update(overrides)
        return base

    def test_failed_save_still_shows_saved_endpoint_id(self):
        status = self._status_for(self._base(serverless_endpoint_id="ep1"))
        self.assertEqual(status["endpoint_id"], "ep1")

    def test_deploy_record_endpoint_is_shown_when_present(self):
        saved = self._base(deploy={"status": "deployed", "endpoint_id": "ep2", "message": "ok"})
        self.assertEqual(self._status_for(saved)["endpoint_id"], "ep2")

    def test_no_endpoint_is_shown_as_none(self):
        self.assertIsNone(self._status_for(self._base())["endpoint_id"])


if __name__ == "__main__":
    unittest.main()
