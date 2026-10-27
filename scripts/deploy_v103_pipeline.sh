#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE="sanjayuk80603/video-summarizer-worker:v1.0.4"
ENDPOINT="4jxzzc08sr5z0w"
LOG="$ROOT/output/_deploy_v103.json"
mkdir -p "$ROOT/output"

log_json() { python3 -c "import json,sys; print(json.dumps(sys.argv[1], indent=2))" "$1" > "$LOG" && cat "$LOG"; }

wait_build() {
  while pgrep -f "docker build.*$IMAGE" >/dev/null 2>&1; do sleep 15; done
}

BUILD_START=$(date +%s)
if pgrep -f "docker build.*$IMAGE" >/dev/null 2>&1; then
  wait_build
fi
# Build whenever the image for this tag is missing locally, even after waiting on another build.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build --platform linux/amd64 -f runpod_worker/Dockerfile -t "$IMAGE" .
fi
BUILD_END=$(date +%s)
BUILD_SEC=$((BUILD_END - BUILD_START))

PUSH_START=$(date +%s)
docker push "$IMAGE"
PUSH_END=$(date +%s)
PUSH_SEC=$((PUSH_END - PUSH_START))

docker manifest inspect "$IMAGE" >/dev/null

export PYTHONPATH="$ROOT"
python3 <<PY
import json, time, sys
from pathlib import Path
ROOT = Path("$ROOT")
IMAGE = "$IMAGE"
ENDPOINT = "$ENDPOINT"
report = {
    "image": IMAGE,
    "endpoint_id": ENDPOINT,
    "build_seconds": $BUILD_SEC,
    "push_seconds": $PUSH_SEC,
}
from src.gpu_selection import load_env_file
load_env_file()
from src.runpod_serverless import patch_serverless_endpoint, get_serverless_endpoint, run_warmup_cold_start
from src.serverless_model_catalog import load_saved_serverless_models
from src.serverless_worker_image_publish import merge_env_file

merge_env_file({"RUNPOD_SERVERLESS_IMAGE": IMAGE})
before, _ = get_serverless_endpoint(ENDPOINT)
report["endpoint_image_before"] = (before or {}).get("image")
t0 = time.time()
patch_serverless_endpoint(ENDPOINT, {"image": IMAGE})
report["endpoint_patch_seconds"] = round(time.time() - t0, 1)
after, _ = get_serverless_endpoint(ENDPOINT)
report["endpoint_image_after"] = (after or {}).get("image")

models = load_saved_serverless_models()
logs = []
t0 = time.time()
result = run_warmup_cold_start(
    ENDPOINT,
    whisper_model=models.get("whisper") or "base",
    ollama_model=models.get("ollama") or "llama3.2:3b",
    nllb_model=models.get("nllb") or "facebook/nllb-200-distilled-600M",
    log=logs.append,
)
report["warmup_seconds"] = round(time.time() - t0, 1)
status = result.get("status")
output = result.get("output")
ok = status == "COMPLETED" and isinstance(output, dict) and output.get("ok")
report["warmup_status"] = "success" if ok else "failed"
report["warmup_runpod_status"] = status
report["warmup_output"] = output
report["warmup_error"] = None if ok else (result.get("error") or output)
report["warmup_log_tail"] = logs[-12:]
report["final"] = "success" if ok else "deployed_warmup_failed"
report["total_seconds"] = report["build_seconds"] + report["push_seconds"] + report["endpoint_patch_seconds"] + report["warmup_seconds"]
Path("$LOG").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
sys.exit(0 if ok else 1)
PY
