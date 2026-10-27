# 7. Setup, Security and Testing

[← Logic and Structure](06-logic-and-structure.md) · [Index](../PROJECT_DOCUMENTATION.md) · Next: [History, Limits and Results →](08-history-limits-results.md)

Sections 10 (setup and deployment), 11 (security and reliability) and 12 (testing and QA).

---

# 10. Setup and Deployment

## 10.1 Prerequisites

| Item | Needed for | Evidence |
|---|---|---|
| Python **3.10 or newer** (3.11 is what CI and the locks use) | Everything | Code uses `X \| None` (`config.py:22`, `app.py:290`); CI uses 3.11 (`.github/workflows/tests.yml:25`). The docs say 3.9+ (`SETUP.md:10`), which is wrong. |
| FFmpeg with the **libass** `subtitles` filter | Highlight videos | `src/highlight_ffmpeg.py:107-120`. macOS: `brew install ffmpeg-full`; Windows: a full build; Linux: `libavfilter-extra` (`src/highlight_ffmpeg.py:13-22`). A plain FFmpeg works for the text path. |
| Ollama with `llama3.2:3b` | Local summaries and local highlight planning | `config.py:42`, `SETUP.md:21-46`. |
| Optional: `qwen2.5:7b-instruct` | Alternative model | `SETUP.md:38-41`. |
| Node.js (optional) | Browser-logic tests only | Tests skip without it; version NOT FOUND IN CODE. |
| Docker (optional) | Building the GPU worker image | `runpod_worker/Dockerfile`; `--platform linux/amd64`. |
| RunPod account and API key (optional) | GPU path | `RUNPOD_API_KEY` (name only, see 10.4). |
| AWS account with a named profile (optional) | S3 storage | `src/s3_video_store.py:29`, `:92-93`. |
| An external "subtitle-gen" folder (optional) | Term correction in transcription | `config.py:13`, `src/transcriber.py:8-19`. The code degrades without it (`src/transcriber.py:15`). |

## 10.2 Install

Two ways, both in the repo:

**A. Quick install (what `SETUP.md` shows):**

```bash
cd video-summarizer
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt     # SETUP.md:59-68
ollama pull llama3.2:3b
```

This installs unpinned versions, which can drift.

**B. Locked install (what CI uses):**

```bash
python -m pip install uv
uv pip install --system --require-hashes -r requirements/mac-cpu.lock      # macOS
uv pip install --system --require-hashes -r requirements/windows-cpu.lock  # Windows
```

(`.github/workflows/tests.yml:28-36`). `scripts/run_local_mac.sh` uses a `venv-mac` folder and the hashed lock files.

NOT FOUND IN CODE: a documented Linux install path. CI does not run on Linux.

## 10.3 Run

| What | Command | Result |
|---|---|---|
| Web app | `python3 app.py` (`app.py:1231-1237`) | Open http://127.0.0.1:5000 |
| Web app in debug mode | `VIDEO_SUMMARIZER_DEBUG=1 python3 app.py` (`app.py:1235`) | Flask debugger on |
| Command line, text only | `python3 main.py "https://www.youtube.com/watch?v=VIDEO_ID" --quality 360 --model llama3.2:3b` (`SETUP.md:106-107`) | Writes into `output/` |
| Command line, with highlights | `python3 main.py <URL or file> --highlight-video --highlight-languages en,hi` (`docs/AI_HANDOFF_CONTEXT.md`, section 10) | Adds highlight MP4s |
| Ollama check | `python3 main.py --test-ollama` (`PROJECT_OVERVIEW.md`) | Checks the Ollama connection |
| Transcript only | `python3 download_transcript.py <URL or file>` (`download_transcript.py:224`) | Writes transcript files |
| Local launcher (macOS) | `scripts/run_local_mac.sh` | Blanks `RUNPOD_API_KEY` so RunPod is never used by accident |
| Local launcher (Windows) | `scripts/run_local_windows.ps1` | Same idea on Windows |

## 10.4 Environment variables

Names, purpose and default only. **No values are shown.** Keep real values in `.env`, which is gitignored (`.gitignore:6`).
`.env.example` lists a subset (`.env.example`, 13 lines). Variables read by the code but missing from the template are noted.

### Core and RunPod

| Name | Purpose | Default if unset |
|---|---|---|
| `RUNPOD_API_KEY` | RunPod API key for all RunPod calls | Required for GPU features; the app raises an error if missing (`src/runpod_serverless.py:65-67`) |
| `RUNPOD_COMPUTE_MODE` | `serverless` or `pod` | `serverless` (`src/compute_deploy.py:40`) |
| `RUNPOD_DATA_CENTER_ID` | Data centre for volumes and endpoints | Required for serverless (`src/runpod_serverless.py:115-119`) |
| `RUNPOD_SERVERLESS_IMAGE` | Worker image reference | None; set by the publish step or by hand |
| `RUNPOD_SERVERLESS_IMAGE_NAME` | Image repository name | `video-summarizer-worker` (`src/serverless_worker_image_publish.py:155`) |
| `RUNPOD_SERVERLESS_IMAGE_TAG` | Image tag | `v1.0.11` (`src/serverless_worker_image_publish.py:18`) |
| `RUNPOD_SERVERLESS_NAME` | Endpoint name | `video-summarizer` (`src/runpod_serverless.py:151`) |
| `RUNPOD_SERVERLESS_GPU_POOLS` | GPU pools offered | `ADA_24,AMPERE_16` (`src/runpod_serverless.py:107`) |
| `RUNPOD_NETWORK_VOLUME_ID` | Reuse an existing network volume | None (`src/compute_deploy.py:273`) |
| `DOCKER_HUB_USER` | Docker Hub account for pushing the worker image | None; fallbacks `DOCKERHUB_USER`, `DOCKER_REGISTRY_USER`, `DOCKER_USER` (`src/serverless_worker_image_publish.py:73-84`) |
| `RUNPOD_HIGHLIGHT_LOCAL` | `1`/`true`/`yes` renders highlights locally even when serverless is on | Off (`src/serverless_model_catalog.py:84`) |
| `RUNPOD_REMOTE_TRANSPORT` | Pod mode: `auto`, `ssh`, `jupyter` (`http` maps to Jupyter) | `auto` (`src/pipeline_executor.py:332`) |
| `RUNPOD_POD_ID` | Pod mode fallback pod ID for Jupyter | None (`src/runpod_jupyter.py:162`) |
| `RUNPOD_JUPYTER_PASSWORD` | Pod mode Jupyter password override | Read from the pod's environment; never printed (`src/runpod_jupyter.py:172`) |
| `RUNPOD_SSH_IDENTITY` | Pod mode SSH private key path | Required for SSH (`src/runpod_ssh.py:103`) |
| `RUNPOD_SSH_TARGET` | Pod mode SSH target override | None (`src/runpod_ssh.py:80`) |
| `RUNPOD_SSH_PROBE_TIMEOUT`, `RUNPOD_SSH_CONNECT_TIMEOUT`, `RUNPOD_SSH_RETRIES`, `RUNPOD_SSH_RETRY_DELAY`, `RUNPOD_SSH_PREFER_DIRECT` | Pod SSH tuning | `25`, `180`, `3`, `5`, `1` (`src/runpod_ssh.py:54-149`) |
| `RUNPOD_REMOTE_APP_ROOT`, `RUNPOD_REMOTE_JOBS_ROOT` | Pod folder layout | `/workspace/video-summarizer`, `/workspace/vsumm-jobs` (`src/runpod_ssh.py:498-505`) |
| `RUNPOD_HEAVY_TIMEOUT_SEC` | Pod heavy-phase timeout | `7200` (`src/runpod_jupyter.py:478`) |
| `RUNPOD_OLLAMA_HOST` | Ollama host on the pod | `http://127.0.0.1:11434` (`src/pipeline_runtime.py:112`) |
| `RUNPOD_WORKER_TRANSCRIBE_ONLY`, `NLLB_VIA_RUNPOD` | Set by the app for pod and NLLB routing; do not set by hand | Unset |
| `RUNPOD_POD_IMAGE`, `RUNPOD_POD_NAME`, `RUNPOD_POD_DISK_GB`, `RUNPOD_POD_PORTS`, `RUNPOD_START_SSH`, `RUNPOD_START_JUPYTER` | Pod creation settings | Image `runpod/pytorch:2.4.0-...`, name `video-summarizer`, 40 GB, `8888/http,22/tcp,8765/http`, `1`, `1` (`src/compute_deploy.py:158-187`) |

### Processing and highlights

| Name | Purpose | Default |
|---|---|---|
| `OLLAMA_HOST` | Local Ollama URL | `http://localhost:11434` (`config.py:41`) |
| `WHISPER_MODEL` | Whisper model size | `base` (`config.py:16`) |
| `WHISPER_MODEL_SHORT` | Short-clip model | `tiny` (`config.py:17`) |
| `FFMPEG_PATH` | Path to the FFmpeg program (must be a file, not a folder) | Searched automatically (`src/highlight_ffmpeg.py:25-62`) |
| `HIGHLIGHT_TRANSLATOR` | Primary translator | `google` (`config.py:65`) |
| `HIGHLIGHT_TRANSLATION_CHAIN` | Full translator order, comma-separated | `google,nllb,ollama` (`src/translators/translation_chain.py:9-27`) |
| `HIGHLIGHT_TRANSLATION_MODEL` | Ollama model for translation | Same as `SUMMARIZATION_MODEL` (`config.py:66`) |
| `HIGHLIGHT_EXTRA_PROTECTED_TERMS` | Extra terms kept untranslated | Empty (`config.py:68-70`) |
| `HIGHLIGHT_RENDER_MAX_PARALLEL_LANGS` | Languages rendered at once (1–4) | Based on CPU count (`config.py:77-92`) |
| `HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC` | Delay between Google calls (minimum 1.0) | `1.0` (`config.py:93`) |
| `HIGHLIGHT_DISABLE_GOOGLE` | Trips the Google breaker at start | Off (`src/highlight_pipeline.py:362`) |
| `HIGHLIGHT_BLOCK_LIVE_GOOGLE`, `HIGHLIGHT_LIVE_GOOGLE` | Test guards for live Google calls | Empty (`src/translators/google_guard.py:7-12`) |

### S3 (required)

| Name | Purpose | Default |
|---|---|---|
| `VIDEO_S3_WORKER_URL_SUPPORT` | Only for RunPod highlights: lets the worker download the source with a signed link | Off (`src/s3_video_store.py`, `worker_url_support`) |
| `VIDEO_S3_BUCKET` | Bucket name | A hard-coded default exists (`src/s3_video_store.py:27`), not reproduced here |
| `VIDEO_S3_REGION` | Region | `ap-south-1` (`src/s3_video_store.py:91`) |
| `VIDEO_S3_PROFILE` | Named AWS profile | Fixed default name (`src/s3_video_store.py:29`) |
| `VIDEO_S3_PRESIGN_EXPIRES` | Worker link lifetime, seconds | `3600`, clamped 60 to 604800 (`src/s3_video_store.py:81-96`) |

`AWS_PROFILE` is read deliberately never (`src/s3_video_store.py:92-93`).

### App behaviour

| Name | Purpose | Default |
|---|---|---|
| `VIDEO_SUMMARIZER_DEBUG` | Flask debug mode (`1`, `true`, `yes`, `on`) | Off (`app.py:1235`) |

### Variables read by the code but not in `.env.example`

`FFMPEG_PATH`, `VIDEO_SUMMARIZER_DEBUG`, `RUNPOD_HIGHLIGHT_LOCAL`, `RUNPOD_REMOTE_TRANSPORT`, the `RUNPOD_SSH_*` and
`RUNPOD_POD_*` settings, the `HIGHLIGHT_*` settings, `WHISPER_*`, and all `VIDEO_S3_*` settings are read in code but are
missing from the template. The list is not exhaustive. NOT FOUND IN CODE: a complete, maintained variable reference.

`.env.example` also lists `RUNPOD_NETWORK_VOLUME_ID` twice (`.env.example:7` and `:13`).

## 10.5 Build the GPU worker image

```bash
docker build --platform linux/amd64 -f runpod_worker/Dockerfile \
  -t <DOCKER_HUB_USER>/video-summarizer-worker:v1.0.11 .
docker push <DOCKER_HUB_USER>/video-summarizer-worker:v1.0.11
```

Replace the placeholders with your own values. The app can do the same from the GPU page
(`src/serverless_worker_image_publish.py:205-330`).

**Build prerequisite that is easy to miss:** the Dockerfile copies `.cache/worker-wheels` (`runpod_worker/Dockerfile:20`),
but `.cache/` is gitignored (`.gitignore:22`) and not tracked. A fresh clone cannot build the image until that folder is
recreated. NOT FOUND IN CODE: the command or script that creates it. The Dockerfile comments say it is built from the same
lock file (`runpod_worker/Dockerfile:12`).

Image contents (`runpod_worker/Dockerfile`):
- base `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` (line 1);
- FFmpeg, curl, zstd (apt, unpinned, lines 6–9) and Ollama (installer script, unpinned, line 9);
- Python packages from `requirements/worker.lock` installed offline with hashes (lines 17–24), gTTS with `--no-deps`;
- an assertion that torch is 2.4.1 and transformers is 4.49.0 (line 24);
- `config.py`, `src/`, fonts, and `runpod_worker/handler.py` copied in (lines 26–34);
- fonts registered with `fc-cache -f` (lines 28–33);
- start command: Ollama in the background, a fixed three-second wait, then the handler (line 40).

## 10.6 Deploy to RunPod

1. Open http://127.0.0.1:5000/gpu-selection.
2. Pick a GPU and models, and choose the worker image (build or registry).
3. Press **Save selection & deploy**. The page warns that a build can take 15–30 minutes (`templates/gpu_selection.html`,
   per the review pass).
4. The endpoint is created at maximum three workers, then set to zero after verification (`src/compute_deploy.py:376-380`).
5. Watch the status banner (section 8.16). "Stop workers" sets the maximum to zero; "Delete network volume" needs the word
   `DELETE` typed to confirm (`static/js/deploy_status.js:331-358`).

Pod mode (legacy) is selected by `RUNPOD_COMPUTE_MODE=pod` or the saved mode. It creates a pod with SSH and Jupyter
(`src/compute_deploy.py:170-208`).

## 10.7 Hosting and environments

| Environment | Where | Evidence |
|---|---|---|
| Web app (development and use) | User's laptop, `127.0.0.1:5000` | `app.py:1237` |
| GPU worker | RunPod serverless endpoint, scale 0 to 3 | `src/runpod_serverless_endpoint_config.py:11-15` |
| Worker image | Docker Hub (or another registry) | `src/serverless_worker_image_publish.py` |
| Storage (optional) | Amazon S3 bucket | `src/s3_video_store.py` |
| CI | GitHub Actions, macOS and Windows runners | `.github/workflows/tests.yml` |
| Production hosting for the web app | NOT FOUND IN CODE: the app is not deployed to any public host |  |

## 10.8 CI/CD

- **Workflow:** `.github/workflows/tests.yml`. Triggers on push, pull request and manual run (lines 3–6).
- **Matrix:** `macos-14` and `windows-latest` (line 16). No Linux runner.
- **Steps:** checkout; Python 3.11; install `uv`; install the hashed lock for the platform; run
  `python -m unittest discover -s tests -v` (lines 21–39).
- **Environment:** `RUNPOD_API_KEY` set to a placeholder value (line 19). Real RunPod calls are not made by the tests.
- **Caching:** none.
- **Deployment:** no CD. Worker images and endpoints are published by hand or from the GPU page.
- **Not in CI:** FFmpeg with libass (so the render test skips unless the runner has it, NOT FOUND IN CODE), Node (JavaScript
  tests skip unless present), Docker, and the worker image.

---

# 11. Security and Reliability

## 11.1 Authentication and access

- **No user login or API key** for the web app (checked by search). NOT FOUND IN CODE: any access-control design.
- **Network exposure:** the Flask server listens on `127.0.0.1` only (`app.py:1237`). Other machines on the network cannot
  reach it, but any program or user on the same machine can.
- **Outbound credentials:** RunPod key, AWS profile, Docker Hub user and optional SSH key or Jupyter password are read from
  the environment or from named profiles. The code does not print them; redaction is in `src/log_safety.py`.

## 11.2 Input validation

| Input | Check in code | Gap |
|---|---|---|
| Upload extension | Lowercase suffix in an allow-list (`app.py:800-803`) | Not the MIME type; not the file contents |
| Upload size | 500 MB request limit (`app.py:79`) | No custom error page for too-large uploads (review pass) |
| Upload filename | **None.** The client filename is used as given (`app.py:806`) | A crafted name is not sanitised; path effects were not tested (see 11.6) |
| YouTube URL | Host must be `youtube.com` or `youtu.be` for the YouTube path (`src/downloader.py:33`) | Other text goes to the local-file handler (`app.py:348-349`) |
| `compression` | **Not validated** (`app.py:784`); unknown values fall back to `medium` in the summariser (`src/summarizer.py:242`) | Accepts any string |
| `model` | Ignored; the server always uses `SUMMARIZATION_MODEL` (`app.py:783`) | None |
| Highlight languages | Must be in `LOCALES` (`app.py:786-790`, `app.py:940`) | None |
| `job_id` on highlight routes | Eight lowercase hex characters (`app.py:1067`, applied at `:1021`, `:1092`, `:1131`, `:1163`) | Not applied to summary, subtitle, audio, results or progress routes |
| `job_id` on summary, subtitle, audio routes | **Not checked** (`app.py:936-1008`) | See 11.6 |
| Retry request body | Language list passed on; other fields not checked (`app.py:1038-1044`) | None |
| Pipeline runtime | Must be a known value (`app.py:650-653`) | None |

## 11.3 Secrets handling

- `.env` is gitignored (`.gitignore:6`). The template `.env.example` has no real values.
- AWS credential CSV file is gitignored (`.gitignore:27`).
- `data/compute_selection.json` and `data/endpoint_leases/` are gitignored (`.gitignore:7`, `:24`). The selection file
  holds deploy identifiers.
- Signed S3 links are not stored in files and are generated on each request (`src/s3_video_store.py:300-355`).
- The worker source link is included in the `/run` request body (`src/pipeline_executor.py:257`). It is not logged.
- Log redaction removes signed-URL queries, passwords, tokens, cookies and key IDs (`src/log_safety.py:54`), and a static
  test scans for unsafe logging calls (`tests/test_log_safety.py`).
- Limit stated in the module: a secret with no recognisable label or shape is not caught (`src/log_safety.py:7-8`).

## 11.4 Encryption

- **In transit:** HTTPS to RunPod, S3, YouTube, Google and Docker Hub. The local web app is plain HTTP on localhost.
- **At rest in S3:** server-side encryption with S3-managed keys (AES-256) (`src/s3_video_store.py:151-158`).
- **At rest on disk:** not encrypted by the app. Job folders contain full videos and transcripts.

## 11.5 Error handling and logging

- **Errors shown to users** are classified and sanitised (`src/youtube_errors.py`, `src/log_safety.py`). A job's error text is
  sanitised when it is recorded (`app.py:218`).
- **Actionable errors** return HTTP 422 with their own message; other failures return HTTP 500 (`app.py:884-896`).
- **Worker failures** mark every requested language as failed, with no silent local fallback (`src/pipeline_executor.py:279-294`).
- **Retries** are limited and deliberate (see 11.7).
- **Logging:** the code prints progress to the console and writes job log lines (`app.py:183`). NOT FOUND IN CODE: a
  logging framework or log levels.

## 11.6 Known security gaps (from the review passes)

Ordered roughly by how much they matter for a single-user laptop app. None of these was exploited or tested here.

| # | Gap | Where | Impact |
|---|---|---|---|
| S1 | Upload filename used unsanitised as the saved path | `app.py:806` | A crafted filename could write outside the upload folder. **Not tested.** Use `werkzeug.utils.secure_filename` or a generated name. |
| S2 | `job_id` not validated on summary, subtitle, audio routes | `app.py:936-1008` | Flask's default segment rule blocks `/`, but `..` is accepted, so reads are not confined to `output/` by the route. **Not tested.** |
| S3 | No authentication on any route | All of `app.py` | Any local process can start jobs, read results, and stop GPU workers (`/api/compute-deploy-stop`) |
| S4 | "Stop workers" and "Delete network volume" bypass the capacity lease | `src/compute_deploy.py:479-504`, `:530-534` | Can stop workers while a job is running (UNCONFIRMED impact) |
| S5 | Pod mode: `job_id` embedded in code run on the pod, without the check the SSH path uses | `src/runpod_jupyter.py:417-421`, `:465`; check at `src/runpod_ssh.py:36-40` | Practical risk UNCONFIRMED; job IDs appear to be app-generated |
| S6 | Unused pod HTTP server binds `0.0.0.0` with no auth; the file route builds paths from the URL; `tarfile.extractall` without a filter | `scripts/runpod_heavy_http_server.py:19`, `:53-67`, `:80`, `:118` | Only if it is run (it has no callers found in the app) |
| S7 | Front-end Markdown library loaded from a CDN without an integrity hash | `templates/index.html:8` | A compromised CDN could change page behaviour |
| S8 | The "Download Audio" button downloads a highlight video for non-English languages | `static/js/app.js:896-905` | Mislabelled download, not a security flaw |
| S9 | Presigned source URL in the `/run` body may expire while the job waits in the queue | `src/pipeline_executor.py:257`, `src/runpod_serverless_payload.py:397-408` | Queue wait times UNCONFIRMED; failed downloads on the worker |
| S10 | Dockerfile installs unpinned apt packages and the Ollama installer, and waits a fixed three seconds before the handler | `runpod_worker/Dockerfile:6-9`, `:40` | Build and start-up are not reproducible or checked |
| S11 | RunPod error text can include HTML, which was shown to users | Fixed in `src/runpod_serverless.py` (short gateway message); old stored messages still contain the HTML | Cosmetic and information leak in old job files |

## 11.7 Reliability: what happens when things fail

| Failure | Behaviour | Source |
|---|---|---|
| Server restarts during a job | In-memory job is lost; files remain; history reloads from disk | `app.py:120-160`, `app.py:81` |
| YouTube blocks a client | Next client profile is tried; up to three attempts | `src/downloader.py:25-30`, `:212-255` |
| YouTube rate limit (429) | Retried | `src/youtube_errors.py` |
| YouTube sign-in or private video | Not retried; user told to upload the file | `src/youtube_errors.py:14-19` |
| Google rate limit | Retried with growing waits, then the circuit breaker stops Google for the job | `src/translators/google_backend.py:199-219`, `src/translators/google_circuit.py` |
| Google other error | Retried up to five times | `src/translators/google_backend.py:27-28`, `:212-219` |
| Translation backend fails | Next backend in the chain | `src/highlight_translate.py:280-307` |
| LLM returns bad scene plan | Retried once per chunk, then evenly spaced fallback | `src/topic_segmentation.py:192-217`, `:329-351` |
| Edge TTS fails | gTTS fallback; if that fails, an **empty** file and a success report | `src/tts_generator.py:219-243`, `src/highlight_tts.py:118-130` |
| One highlight language fails | That language is marked failed; others continue | `src/highlight_pipeline.py:387-399`, `:441-456` |
| Shared highlight stage fails | Whole highlight phase stops; no per-language records | `src/highlight_pipeline.py:408-413` |
| Missing glyph in a caption font | Language fails with a clear message | `src/highlight_assembler.py:205-252` |
| RunPod job rejected as paused (409) | Retried every 10 s for up to 240 s | `src/runpod_serverless.py:264-265` |
| RunPod gateway error (502–504) on submit | **Not retried** (possible duplicate job); failed with a short message | `src/runpod_serverless.py`, `submit_job` |
| RunPod job fails or times out | Failed; no local fallback | `src/pipeline_executor.py:279-294` |
| Capacity lease expires (4 h) | Lease ignored; sweep sets zero workers | `src/endpoint_capacity.py:37`, `:98-108` |
| S3 upload fails | Highlights not started remotely; each language marked `s3_upload` | `src/pipeline_executor.py:260-266` |
| S3 delete fails | Record `delete_failed`; key kept for manual cleanup | `src/pipeline_executor.py:32-44` |

## 11.8 Rate limits

- **On the web app:** none. Any local client can start unlimited jobs (checked by search for rate-limit code).
- **Against external services:** Google translation is throttled to one request per second or slower
  (`src/translators/google_backend.py:55-68`); YouTube has client fallbacks but no explicit throttle.
- **RunPod:** the endpoint has a queue and a maximum of three workers (`src/runpod_serverless_endpoint_config.py:11-15`).

## 11.9 Backups

NOT FOUND IN CODE: any backup routine for job folders, leases, or the selection file. Losing `output/` loses all results.
The S3 bucket is versioned only if configured outside the code (UNCONFIRMED).

## 11.10 Monitoring

- Progress and log lines in the browser (`static/js/app.js`).
- RunPod counts and state on the GPU page (section 8.16).
- NOT FOUND IN CODE: metrics export, alerting, or uptime checks.

---

# 12. Testing and QA

## 12.1 Test types and inventory

| Type | Count | Where |
|---|---|---|
| Unit tests (Python) | Most of the 420 test methods | `tests/test_*.py` |
| Browser-logic tests (JavaScript run in Node) | 52 tests in 6 files | `tests/test_gpu_status_banner_js.py`, `test_gpu_status_poller_js.py`, `test_highlight_retry_js.py`, `test_highlight_rows_js.py`, `test_poll_and_results_js.py`, `test_progress_view_js.py` |
| Rendering test (real FFmpeg with libass) | 10 tests in one file | `tests/test_subtitle_fonts.py` |
| Concurrency and locks | Cross-process file lock test | `tests/test_locking_platform.py` |
| Integration-style (app routes) | Several files import `app` | e.g. `tests/test_highlight_requested.py`, `tests/test_log_safety.py`, `tests/test_live_status_integration.py` |
| Static code check | `find_unsafe_logging` scans source for unsafe log calls | `tests/test_log_safety.py` |
| Manual smoke and end-to-end scripts | Not run by CI | `scripts/` (section 9.1) |

Totals: **32 test files and 420 test methods** (`def test_` count). Run during this documentation pass: 420 tests, all passing,
3 skipped (see 12.4).

Per-file purposes are in the review pass summary; the most important ones:
- `test_endpoint_capacity.py` (35): lease logic, threads, file locks.
- `test_s3_highlight_links.py` (50): signed links, expiry, error codes.
- `test_log_safety.py` (27): redaction patterns and the static guard.
- `test_progress_fixes.py` (28): progress rules and the `.dockerignore` contents.
- `test_s3_video_storage.py` (28): S3 source storage.
- `test_subtitle_fonts.py` (10): fonts, glyph detection, production filter path with spaces.
- `test_job_readiness.py` (13): paused endpoint retry, 409 and gateway handling.

## 12.2 How to run

```bash
# All tests (what CI runs)
python -m unittest discover -s tests -v

# One file
cd tests && python -m unittest test_subtitle_fonts -v

# With the libass FFmpeg, so the rendering tests run (macOS example)
FFMPEG_PATH=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg python -m unittest discover -s tests
```

Tests are run from the `tests/` folder or with `-s tests`: there is no `__init__.py` in `tests/`. Each test module adds the
repo root to the import path itself.

## 12.3 What the tests need

| Need | Effect when missing |
|---|---|
| Node.js | The six JavaScript test files skip |
| FFmpeg with libass | The rendering tests skip (`tests/test_subtitle_fonts.py`); the FFmpeg check tests use mocks |
| `runpod` package | `tests/test_remote_highlight.py` skips some tests |
| `gtts` | One highlight TTS test skips |
| Docker | None needed |
| Network | None intended; external services are mocked (per-file check UNCONFIRMED) |
| `RUNPOD_API_KEY` | Tests set a placeholder value (`tests/test_job_readiness.py:13`) |

## 12.4 Results recorded during this documentation pass

- Full Python suite, `venv-mac` with the libass FFmpeg: **420 tests, OK, 3 skipped.** The skips are: two worker tests that
  need the RunPod package on macOS, and one Linux-only test (missing glyphs without fonts).
- The same subtitle tests were run inside the built worker image on Linux: 9 passed, 1 skipped (the Dockerfile wiring test,
  because the Dockerfile is not in the image).
- Browser check (Chrome, Playwright, local app): the highlight-status scenario and checklist behaviour passed with no page
  errors (see [08](08-history-limits-results.md), section 16).

## 12.5 Coverage

NOT FOUND IN CODE: a coverage tool, a coverage percentage, or a coverage configuration. Coverage was not measured.

Modules with **no test file that names them** (name-based check; some are exercised through other modules):
- Summary path: `subtitle_generator.py`, `tts_generator.py`, `audio_extractor.py`, `text_cleaner.py`, `hierarchy_detector.py`,
  `natural_speech_generator.py`, `domain_handler.py`, `summary_jobs.py`, `summary_translate.py`.
- Translation: `term_protection.py`, `simple_indic_translit.py`, `translation_cache.py`, `translation_chain.py`,
  `translation_log.py`, `translation_quality.py`, and most of `translators/`.
- Highlight: `highlight_captions.py`, `highlight_chunking.py`, `highlight_llm.py`, `highlight_shared_video.py`,
  `highlight_timing.py`, `highlight_translate.py`, `job_video.py`, `narration_rewrite.py`, `topic_segmentation.py`,
  `visual_extractor.py`.
- RunPod and GPU: `runpod_heavy_core.py`, `runpod_http.py`, `runpod_jupyter.py`, `runpod_ssh.py`, `runpod_nllb_proxy.py`,
  `gpu_selection.py`, `gpu_recommendations.py`, `pipeline_model_status.py`.
- Entry points: `main.py` and `download_transcript.py` have no dedicated test file.
- `scripts/` except `check_video_s3.py` (its test is `test_s3_check_script.py`).

## 12.6 Known testing gaps

- No end-to-end test of a full job (download → summary → highlights) in CI. Real runs were done by hand.
- No test that calls the real RunPod API; the paid path is untested by CI.
- No Linux CI runner, although the worker is Linux.
- The libass rendering test silently skips on runners without libass, so CI may pass without running it (UNCONFIRMED per runner).
- Browser tests use Node stubs, not a real browser, in the repository. A real-browser check was done once outside the repo.
- Security gaps S1 and S2 (section 11.6) have no tests.
- `transcriber.py` is only patched in one test, never exercised directly.
