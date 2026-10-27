"""RunPod-native billing and lifecycle controls (informational only — not enforced by this app)."""

from __future__ import annotations

from typing import Any

# Standard on-demand Pods (POST https://api.runpod.io/v2/pods) have no inactivity
# auto-stop field in the v2 API. GraphQL stopAfter/terminateAfter are absolute times,
# not idle timers, and are not supported on v2 pod create (per RunPod Terraform migration).
# Serverless endpoints use workers.idleTimeout (seconds) — a different product model.
RUNPOD_IDLE_NATIVE_FOR_STANDARD_PODS = False

# Per-pod dollar caps are not exposed on pod create/update. RunPod provides account-level
# spend limits (default ~$80/hr across resources), low-balance notifications, and stops
# all pods when balance hits $0 — see https://docs.runpod.io/accounts-billing/billing
RUNPOD_PER_POD_SPEND_CAP_NATIVE = False

RUNPOD_BILLING_DOCS_URL = "https://docs.runpod.io/accounts-billing/billing"
RUNPOD_IDLE_MONITOR_REPO = "https://github.com/runpod/Runpod-Idle-Pod-Monitor"
RUNPOD_SERVERLESS_IDLE_DOCS = "https://docs.runpod.io/sdks/graphql/manage-endpoints"


def platform_controls_status() -> dict[str, Any]:
    """Payload for deploy banner / GPU selection (no app-side enforcement)."""
    return {
        "idle_auto_stop_native": RUNPOD_IDLE_NATIVE_FOR_STANDARD_PODS,
        "idle_auto_stop_note": (
            "Standard RunPod pods have no inactivity timer on POST /v2/pods. "
            "Use your cloud console (Idle Pod Monitor template) for account-wide idle stop, "
            "or Serverless endpoints with idleTimeout for scale-to-zero workers."
        ),
        "idle_monitor_url": RUNPOD_IDLE_MONITOR_REPO,
        "serverless_idle_docs_url": RUNPOD_SERVERLESS_IDLE_DOCS,
        "spend_cap_native": RUNPOD_PER_POD_SPEND_CAP_NATIVE,
        "spend_cap_note": (
            "RunPod does not offer a per-pod dollar cap via the Pod API. "
            "Use account balance, low-balance alerts (Settings → Notifications), "
            "and the default account spend limit (~$80/hr; contact support to raise)."
        ),
        "billing_docs_url": RUNPOD_BILLING_DOCS_URL,
    }
