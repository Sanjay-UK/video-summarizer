# RunPod Serverless (video-summarizer)

Heavy GPU work (Whisper, Ollama summarize, NLLB) and **highlight video** (Edge TTS, FFmpeg assembly) run on a **RunPod Serverless** queue endpoint when `highlight_rendering` is `serverless` (default). Flask stays local for download, audio extract, and orchestration. Workers **scale to zero** when idle; billing is controlled on RunPod, not by whether your laptop is on.

## Prerequisites

1. **RunPod API key** in `.env`: `RUNPOD_API_KEY=...`
2. **Data center** (network volumes are pinned): `RUNPOD_DATA_CENTER_ID=US-KS-2` (example)
3. **Worker Docker image** on Docker Hub (or set `RUNPOD_SERVERLESS_IMAGE` manually if already pushed)
4. Optional: `DOCKER_HUB_USER=...` if `docker login` is not configured
5. Optional: reuse volume `RUNPOD_NETWORK_VOLUME_ID=...` (auto-filled from saved deploy)

**GPU page → Worker Docker image**

| Mode | Docker on laptop? | What happens |
|------|-------------------|--------------|
| **Custom worker** (default) | Yes | Auto build/push `runpod_worker/Dockerfile`, set `RUNPOD_SERVERLESS_IMAGE`, deploy + warmup |
| **Existing registry image** | No | Paste `docker.io/user/repo:tag` (from Docker Hub or a Hub/GitHub build); deploy + warmup |

**RunPod Hub** ([Whisper](https://console.runpod.io/hub/runpod-workers/worker-faster_whisper), [Ollama](https://console.runpod.io/hub/runpod-workers/worker-ollama), community NLLB translate repos) deploy **separate** serverless endpoints with **different APIs**. They are not a single image for this app's `heavy_phase` / `nllb_batch` / `warmup` jobs. Use Hub to build/host **our** Dockerfile (no local Docker), then paste the resulting image ref — or deploy Hub workers as standalone services outside this Flask app.

Hub deploy without local Docker: RunPod console → Hub → Deploy (RunPod builds from GitHub). For **this** repo, point Hub at `runpod_worker/Dockerfile` or use **Custom worker** auto-build.

Manual build (optional):

```bash
docker build --platform linux/amd64 -f runpod_worker/Dockerfile -t YOUR_USER/video-summarizer-worker:v1.0.0 .
docker push YOUR_USER/video-summarizer-worker:v1.0.0
```

## Endpoint configuration (RunPod canonical values)

These are applied in code via `src/runpod_serverless_endpoint_config.py` when creating the endpoint. They are **not** tied to local machine uptime.

| Setting | Value | RunPod reference |
|--------|--------|------------------|
| Active workers (`workers.min`) | **0** | [Endpoint configurations](https://docs.runpod.io/serverless/endpoints/endpoint-configurations) default |
| Max workers (`workers.max`) | **3** | Platform default |
| Idle timeout | **5 s** | Platform default (worker scales down after idle) |
| Execution timeout (`timeout`) | **3_600_000 ms (1 h)** | Default is 600 s; raised for long lecture transcribe+summarize (GPU job duration, not laptop) |
| Scaling | **QUEUE_DELAY**, queue delay **4 s** | Platform defaults |
| FlashBoot | **FLASHBOOT** | Enabled per platform default |
| Worker env | **`RUNPOD_INIT_TIMEOUT=800`** | When multi-model cold start may exceed ~7 min |

## Deploy from the app

1. Set `RUNPOD_COMPUTE_MODE=serverless` (default).
2. Open **GPU selection** → pick models + GPU + **Worker Docker image** mode → **Save selection & deploy** (auto build/push only when **Custom worker** is selected and `RUNPOD_SERVERLESS_IMAGE` is missing).
3. Creates (if needed) a **network volume** and a **serverless endpoint**; IDs saved in `data/compute_selection.json`.

Legacy pods: set `RUNPOD_COMPUTE_MODE=pod`.

## Jobs and cold start measurement

- Flask submits `POST https://api.runpod.ai/v2/{endpoint_id}/run` and polls `/status/{job_id}`.
- On completion, **`delayTime`** and **`executionTime`** (milliseconds) are logged on the job and stored in `deploy.last_job_metrics` and `output/{job}/runpod_job_metrics.json`.
- RunPod docs: [Job states and metrics](https://docs.runpod.io/serverless/endpoints/job-states), [Benchmarking](https://docs.runpod.io/serverless/development/benchmarking).

## Stop workers / delete volume

- **Stop workers (scale to 0)** — GPU worker billing stops; endpoint remains.
- **Delete network volume** — **Permanent data loss** for cached weights on that volume; confirm with `DELETE` in the UI.

## Payload limit

Async `/run` payload is ~10 MB. Audio and source video are gzip+base64; the app re-encodes when needed to fit (~9 MB safe limit). If video still does not fit, highlight rendering falls back to local FFmpeg. Set `RUNPOD_HIGHLIGHT_LOCAL=1` or `highlight_rendering: local` in saved serverless models to keep highlights on the laptop.

Worker job types: `heavy_phase`, `highlight_phase`, `nllb_batch`, `warmup`.
