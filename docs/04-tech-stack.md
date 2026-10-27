# 4. Tech Stack

[← Architecture](03-architecture.md) · [Index](../PROJECT_DOCUMENTATION.md) · Next: [Data and API →](05-data-and-api.md)

Versions below come from the lock files, which are the exact versions CI and the local launcher install
(`requirements/mac-cpu.lock`, `requirements/windows-cpu.lock`, `requirements/worker.lock`). Where the lock and the loose
requirement differ, both are shown. "Why" is taken from code comments or the handoff notes; where none exists, it is
marked as inferred.

**Everyday analogy for the stack:** a kitchen. Python is the kitchen itself; each library is a specialised appliance
(Whisper is the transcriber, Ollama the writer, FFmpeg the video cutter). Docker is a labelled lunch box that carries
the whole kitchen to a rented GPU.

## 4.1 Languages

| Language | Version | Where used | Why |
|---|---|---|---|
| Python | **3.11** in CI and locks (`.github/workflows/tests.yml:25`, `requirements/mac-cpu.lock:2`, `runpod_worker/Dockerfile:1`); **3.10+ required by code** (`config.py:22` and `app.py:290` use `X \| None` syntax) | Backend, pipelines, worker, tests | The whole backend. **Docs say 3.9+** (`SETUP.md:10`), which is wrong; see [08](08-history-limits-results.md), section 15. |
| JavaScript (browser) | ES2015+ style, no build step | `static/js/app.js`, `static/js/deploy_status.js` | Plain scripts served as-is; no bundler. |
| JavaScript (Node, tests only) | Node version NOT FOUND IN CODE | Browser-logic tests (`tests/test_*_js.py`) | Runs real functions from `app.js` without a browser. |
| HTML and CSS | — | `templates/*.html`, `static/css/style.css` | Jinja2 templates rendered by Flask. |
| Bash and PowerShell | — | `scripts/run_local_mac.sh`, `scripts/run_local_windows.ps1` | Local launchers. |
| Dockerfile syntax | — | `runpod_worker/Dockerfile` | Builds the GPU worker image. |

## 4.2 Web framework and front end

| Item | Version | Purpose | Why (evidence) |
|---|---|---|---|
| Flask | 3.1.3 (mac/windows lock); `>=2.3.0` in `requirements.txt` | Web server and JSON API (`app.py`) | Simple Python web framework; one file for routes. |
| Werkzeug | comes with Flask | Request and upload handling | Part of Flask. |
| Jinja2 templates | with Flask | Page HTML | Built in. |
| marked (JavaScript) | loaded from `cdn.jsdelivr.net` with no integrity hash (`templates/index.html:8`) | Renders the summary Markdown in the browser | Lightweight; a noted risk in [07](07-setup-security-testing.md). |
| Vanilla JavaScript | — | UI logic, polling, rendering | No framework; the UI is small. |

## 4.3 Speech, language and AI models

| Item | Version / model | Purpose | Why (evidence) |
|---|---|---|---|
| faster-whisper | 1.2.1 (locks) | Speech-to-text with timestamps (`src/transcriber.py`) | "2–4× faster than Whisper" (`README.md`, Dependencies table). Runs on CPU locally and CUDA on the worker (`runpod_worker/Dockerfile`, `src/runpod_heavy_core.py:35`). |
| Whisper model sizes | `base` default; `tiny` for the short variant (`config.py:16-17`) | Accuracy versus speed | The README recommends `base` for an 8 GB laptop. |
| CTranslate2 | 4.8.3 (worker lock) | Inference engine behind faster-whisper | Installed as a dependency of faster-whisper. |
| Ollama (server) | Installed by a script at build time, unpinned (`runpod_worker/Dockerfile:9`); local version not pinned | Runs the LLMs locally and on the worker | Local LLM with an HTTP API (`OLLAMA_HOST`, `config.py`). |
| Ollama (Python client) | 0.6.3 | Calls the Ollama server | Official client (`src/summarizer.py`, `src/highlight_llm.py`). |
| Summary model | `llama3.2:3b` (`config.py:42`) | Concept extraction and summary writing | Small enough for a laptop; the README mentions `qwen2.5:7b-instruct` as an option (`SETUP.md:38-41`). |
| CLI default model | `llama2` (`main.py:187`) | Only for `main.py` when `--model` is not given | Inconsistent with `config.py:42`; see section 15. |
| NLLB-200 (distilled 600M) | `facebook/nllb-200-distilled-600M` (`src/translators/nllb_backend.py:20`) | Offline translation for many languages | Offline, no per-character fee. |
| transformers | 4.49.0 (pinned) | Loads NLLB | "4.50+ refuses torch.load on torch<2.6 (CVE-2025-32434)" (`runpod_worker/Dockerfile:14`). |
| tokenizers | 0.21.4 | Tokenizing text for NLLB | Pinned with transformers. |
| huggingface-hub | 0.36.2 (`>=0.34.0,<1.0`) | Downloads model files | Dependency of transformers. |
| sentencepiece | 0.2.2 | Tokenizer model support | Needed by NLLB. |
| accelerate | 1.15.0 | Model loading helper | Listed in `requirements/worker.in`. |
| PyTorch | 2.4.1 (mac/windows locks; worker uses the base image's build) | Runs NLLB | Base image provides CUDA torch on the worker (`runpod_worker/Dockerfile:13`). Installing another torch would replace the CUDA build, so the Dockerfile is careful about it. |
| Microsoft Edge TTS (`edge-tts`) | 7.3.1 | Narration voices (online service, no key) | Natural neural voices; supports Hindi, Kannada, Telugu, Tamil (`docs/AI_HANDOFF_CONTEXT.md`, section 4.2). |
| gTTS | 2.5.4 (in `worker-gtts.lock`, installed with `--no-deps`) | Fallback narration | Backup when Edge TTS fails (`src/highlight_tts.py:118-130`). |
| pydub | 0.25.1 | Audio helpers | Used for audio handling in the pipeline. |
| indic-transliteration | 2.3.82 | Transliteration for Indic terms (`src/simple_indic_translit.py`) | Keeps names readable across scripts. |
| deep-translator | 1.11.4 | Google Translate client (`src/translators/google_backend.py`) | Free unofficial client; rate-limited (`docs/AI_HANDOFF_CONTEXT.md`, section 4.3). |

## 4.4 Video and audio tools

| Item | Version | Purpose | Why |
|---|---|---|---|
| FFmpeg (external program) | Not pinned; must include the **libass** `subtitles` filter (`src/highlight_ffmpeg.py:107-120`) | Audio extraction, video scaling, subtitle burn-in, concatenation | Only tool that can burn ASS captions. macOS: `ffmpeg-full` via Homebrew (`src/highlight_ffmpeg.py:13-22`). |
| ffprobe | Ships with FFmpeg | Reads media duration | Found next to FFmpeg (`src/highlight_ffmpeg.py:65-72`). |
| yt-dlp | 2026.10.21 (mac/windows locks) | YouTube download | Maintained downloader with client profiles (`src/downloader.py`). |
| OpenCV | Not used | — | NOT FOUND IN CODE: the visual selection uses Pillow and NumPy, not OpenCV (`src/visual_extractor.py`). |
| Pillow | 13.0.0 | Images, placeholder slides, sharpness scoring | `src/visual_extractor.py:149-174`. |
| NumPy | 2.4.6 (mac/windows); 1.26.3 (worker) | Pixel maths for motion and sharpness | `src/visual_extractor.py:89-131`. The worker pins an older NumPy. |
| Google Fonts / Noto and DejaVu | Bundled files in `static/fonts/` | Captions in each script | See [07](07-setup-security-testing.md), section 11. |

## 4.5 Storage, cloud and infrastructure

| Item | Version / setting | Purpose | Why |
|---|---|---|---|
| Local file system | — | All job artifacts (`output/`, `downloads/`, `data/`) | No database (see [05](05-data-and-api.md)). |
| boto3 | 1.43.118 (locks); `>=1.34.0` in `requirements/local.in` | Amazon S3 upload, download, signed links | Official SDK (`src/s3_video_store.py`). |
| filelock | 4.2.4 (mac/windows); 3.13.1 (worker) | Cross-process locks for leases | `src/endpoint_capacity.py:42`. |
| Amazon S3 | Bucket and region from env; default region `ap-south-1` (`src/s3_video_store.py:91`) | Optional source and highlight storage | Lets the worker download the source without sending it in the request body. |
| RunPod Serverless | REST API `api.runpod.io` (management), `api.runpod.ai` (jobs) | Runs the GPU worker, scales to zero | Pay only while busy; see [03](03-architecture.md), decision D7. |
| RunPod Pods (legacy) | REST API; SSH; Jupyter proxy | Older GPU path | Replaced by serverless, still present (`src/runpod_ssh.py`, `src/runpod_jupyter.py`). |
| Docker | Desktop or CLI, `--platform linux/amd64` | Builds the worker image | The worker runs on x86 GPU machines. |
| Docker Hub | Account from `DOCKER_HUB_USER` | Stores the worker image | Pulled by RunPod endpoints (`src/serverless_worker_image_publish.py`). |
| CUDA | 12.4.1 (base image) | GPU runtime | `runpod_worker/Dockerfile:1`. |
| Base image | `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` | Starting point for the worker | Ships CUDA and torch (`runpod_worker/Dockerfile:1`). |
| Ubuntu | 22.04 (inside the base image) | Worker operating system | Base image name. |

## 4.6 Testing, CI and tooling

| Item | Version | Purpose | Why |
|---|---|---|---|
| unittest | Python standard library | Test runner (`python -m unittest discover -s tests`) | No extra test framework needed. |
| GitHub Actions | `actions/checkout@v4`, `actions/setup-python@v5` | CI on push and pull request (`.github/workflows/tests.yml`) | Free for a public repo. |
| macOS runner | `macos-14` (Apple silicon) | CI platform | `.github/workflows/tests.yml:16`. |
| Windows runner | `windows-latest` | CI platform | `.github/workflows/tests.yml:16`. |
| uv | Latest at install time | Installs hashed lock files quickly (`.github/workflows/tests.yml:28`) | Speed and `--require-hashes` support. |
| pip-tools / uv compile | Command in `requirements/worker.in:3-5` | Creates the lock files | Reproducible pins. |
| Playwright | NOT a repo dependency | Used only in an outside tool folder during the session that produced the latest fixes | Not part of the project. |

## 4.7 External services used at run time

| Service | Used for | Needs an account or key | Evidence |
|---|---|---|---|
| YouTube | Downloading the video | No key; cookies optional | `src/downloader.py` |
| Google Translate (unofficial) | Highlight narration and summary translation | No key in the code path used | `src/translators/google_backend.py` |
| Microsoft Edge TTS | Narration voices | No key | `src/tts_generator.py`, `src/highlight_tts.py` |
| Google gTTS | Fallback narration | No key | `src/highlight_tts.py:118-130` |
| RunPod | GPU compute | `RUNPOD_API_KEY` | `src/runpod_serverless.py` |
| Amazon S3 | Optional storage | AWS named profile | `src/s3_video_store.py` |
| Docker Hub | Worker image | Docker login | `src/serverless_worker_image_publish.py` |
| ollama.com | Installer script at image build time | No key | `runpod_worker/Dockerfile:9` |
| cdn.jsdelivr.net | Front-end Markdown renderer | No key | `templates/index.html:8` |

## 4.8 Tools considered but not used (what the code shows)

| Alternative | Status |
|---|---|
| A relational database (SQLite, Postgres) | Not used. |
| A web framework other than Flask (FastAPI, Django) | Not used. |
| A JavaScript framework (React, Vue) or bundler | Not used. |
| A task queue (Celery, RQ) | Not used. Jobs are daemon threads (`app.py:832-845`). |
| An ORM | Not used. |
| Docker Compose | NOT FOUND IN CODE. |
| Kubernetes or Terraform | Not used by this repo (the RunPod Terraform note is only a comment in `src/runpod_platform_controls.py:9`). |
