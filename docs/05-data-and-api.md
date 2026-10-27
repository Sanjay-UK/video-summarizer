# 5. Data Layer and API

[← Tech Stack](04-tech-stack.md) · [Index](../PROJECT_DOCUMENTATION.md) · Next: [Logic and Structure →](06-logic-and-structure.md)

Sections 6 (data) and 7 (API) of the documentation brief are here together, because the API reads and writes the data.

---

# 6. Data Layer

## 6.1 Storage model in one paragraph

The project has **no database and no migrations** (checked by search of `app.py`, `src/`, `runpod_worker/`, `scripts/`).
Data is stored as **files in folders**, described by **JSON documents**, plus **in-memory Python objects** for live
jobs. Think of it as a filing cabinet: each job gets one folder, and labelled index cards (JSON) say what is inside.
The cabinet is rebuilt from the folders when the app starts (`app.py:120-160`).

## 6.2 Where data lives

| Location | Created by | Contents | Survives restart | Source |
|---|---|---|---|---|
| `output/<job_id>/` | `JobProgress.__init__` (`app.py:180-181`) | All artifacts of one job | Yes | `app.py:180`, `config.py:9` |
| `downloads/<short_title>/` | `src/downloader.py` | Downloaded YouTube video | Yes | `src/downloader.py`, `config.py:7` |
| `<system temp>/videosummarizer_uploads/` | `app.py:83-84` | Uploaded files before processing | Yes, but never cleaned up | `app.py:83-84` |
| `data/compute_selection.json` | GPU page save (`src/compute_deploy.py`) | Saved GPU/deploy choices (IDs, pools, models) | Yes; gitignored (`.gitignore:7`) | `src/compute_deploy.py:21` (`SELECTION_PATH`) |
| `data/endpoint_leases/` | `src/endpoint_capacity.py` | One JSON lease per job holding the endpoint | Yes; gitignored (`.gitignore:24`) | `src/endpoint_capacity.py:58-71` |
| `jobs` dict and `job_history` list | `app.py:81-82` | Live job state for progress polling | **No** | `app.py:81-82` |
| `.cache/worker-wheels/` | Built locally | Wheels for the worker image build | Yes; gitignored (`.gitignore:22`) | `runpod_worker/Dockerfile:20` |
| `static/fonts/`, `static/licenses/` | Committed | Caption fonts and the DejaVu licence | Yes (committed) | `src/locales.py:12` |

Files that must never be committed (gitignored, with the reason given in the file itself):
`.env` (`.gitignore:6`), `data/compute_selection.json` (`.gitignore:7`), `data/endpoint_leases/` (`.gitignore:24`),
and the AWS credential CSV file named in `.gitignore:27`.

## 6.3 Job folder: file-by-file

Written by `app.py` and the pipelines. Not every file exists for every job; the "when" column says when.

| File | When written | Format | Written by | What it holds |
|---|---|---|---|---|
| `source.mp4` | Always (copy of the input video) | Video | `src/job_video.py:11`, `:19` | Source for highlights and downloads |
| `audio.mp3` | Always | MP3 | `src/audio_extractor.py:8`, `:20` | Audio track |
| `transcript.txt` | Always | Text | `src/transcriber.py:147` | Full transcription |
| `segments.json` | Always | JSON list | `src/transcriber.py:152` | Each segment: `start`, `end`, `text` |
| `uncertain_terms.json` | When suspicious words are found | JSON | `src/transcriber.py:158` | Words the term-correction could not fix (for manual review) |
| `summary.txt` | Always | Text | `src/summarizer.py:644` | The summary |
| `summary_segments.json` | Always | JSON list | `src/summarizer.py:649` | Summary sentences with timestamps |
| `summary_metadata.json` | On completion or error | JSON | `app.py:561-571` (completed), `app.py:593-604` (error) | `status`, `error`, `error_code`, timing |
| `summary.srt` | When subtitles are generated | SRT text | `src/subtitle_generator.py:91` | Subtitle file |
| `audio_explanation.mp3` | When narration is generated | MP3 | `src/tts_generator.py:108` | Spoken summary |
| `summary_translations.json` | When a summary is translated | JSON | `src/summary_translate.py:80-81` | Translation status per language |
| `summary_<lang>.txt` | When a summary is translated or downloaded | Text | `src/summary_translate.py:189`, `app.py:950-952` | Translated summary |
| `runpod_job_metrics.json` | After a RunPod heavy job | JSON | `src/pipeline_executor.py:199-205` | `delayTime` and `executionTime` in milliseconds, plus `runpod_job_id` |
| `heavy_done.json` | After a Jupyter/SSH pod run | JSON | Pod worker (`scripts/runpod_heavy_worker.py`) | Marker that the pod finished |
| `manifest.json` (in S3, not on disk) | Every finished or failed job | JSON | `src/s3_video_store.py` (`publish_job`, `publish_failure`) | Job ID, video ID, status, results, and each file's key and size. Written last. Never a signed URL. |
| `s3_video.json`, `s3_highlights.json` | Old local records, no longer written | JSON | none | Left over from the earlier design; ignored. |
| `highlights/highlight_status.json` | When highlights are requested | JSON | `src/highlight_jobs.py:41-55`, `src/highlight_pipeline.py` | Per-language status (section 6.4) |
| `highlights/highlight_scenes.json` | When highlights are built | JSON | `src/highlight_pipeline.py:131` | Scene list (topics) |
| `highlights/visual_manifest.json` | When highlights are built | JSON | `src/visual_extractor.py:327-350` | Per-scene visual mode and scores |
| `highlights/<lang>/highlight_<lang>.mp4` | On language success | MP4 | `src/highlight_pipeline.py` | The deliverable video |
| `highlights/<lang>/language_status.json` | Per language | JSON | `src/highlight_pipeline.py` | One language's result |
| `highlights/<lang>/narration_manifest.json` | Per language | JSON | `src/highlight_pipeline.py` | Narration and timing per scene |
| `highlights/<lang>/translations.json` | Non-English languages | JSON | `src/highlight_pipeline.py` | Translated scene text |
| `highlights/_shared/` | Shared across languages | Mixed | `src/highlight_pipeline.py`, `src/highlight_shared_video.py` | `english_narrations.json`, `translation_cache.json`, `scene_timing.json`, per-scene `visual.mp4` |
| `highlights/translation_log.json` | When highlights are translated | JSON | `src/translation_log.py` | Protected-term and failure log |
| `visuals/` | During highlight build | JPG, MP4, PNG | `src/visual_extractor.py` | Chosen still frame, clip, or placeholder per scene |

## 6.4 Highlight status document (`highlights/highlight_status.json`)

Shape from `src/highlight_jobs.py:41-55` and `src/highlight_pipeline.py:288-297`, `:183-190`. Values are illustrative.

```json
{
  "requested_languages": ["en", "hi"],
  "languages": {
    "en": {
      "language": "en",
      "status": "success",
      "translator": "source",
      "translation_seconds": 0.0,
      "tts_seconds": 12.4,
      "render_seconds": 38.1,
      "total_seconds": 51.0,
      "narration_voices": ["en-US-GuyNeural"]
    },
    "hi": {
      "language": "hi",
      "status": "failed",
      "translator": "google",
      "message": "Google rate limit — try again later.",
      "failure_kind": "rate_limit",
      "translation_seconds": 3.2
    }
  }
}
```

Derived status for the UI (`src/highlight_jobs.py:91-99`):

| Stored record | File on disk | Shown as |
|---|---|---|
| `success` | yes | success (download offered) |
| `failed` | any | failed, with `message` |
| `success` | no | failed |
| none, language requested | — | not generated |
| none, language not requested | — | no row |

Possible `failure_kind` values (review of `src/highlight_translate.py`, `src/highlight_pipeline.py`, `app.py:515-520`,
`src/pipeline_executor.py:75`, `:263`, `:292`, `:309`): `rate_limit`, `error`, `missing_output`, `s3_upload`, `runpod_worker`.

## 6.5 Schemas of other JSON files (brief)

- **`segments.json`** (`src/transcriber.py:152`): list of `{start, end, text}`; times in seconds.
- **`summary_segments.json`** (`src/summarizer.py:649`): list of linked sentences with `start`, `end`, `text`, `score`.
- **`summary_metadata.json`** (`app.py:561-571`): `status`, plus any of `error`, `error_code`, timing values.
- **`compute_selection.json`** (`src/compute_deploy.py`): saved deploy choices. Contains endpoint and volume identifiers
  and model choices. Not shown here because it holds identifiers that belong to the owner's account.
- **Lease file** (`src/endpoint_capacity.py`): one JSON per job with a timestamp; leases older than four hours are ignored
  (`src/endpoint_capacity.py:37`, `:98-108`).

## 6.6 Data sources

| Source | Kind | Where |
|---|---|---|
| User's video file | Upload (MP4, MKV, AVI, MOV, FLV, WMV, WEBM) | `app.py:800`, `:806` |
| YouTube video | Download via yt-dlp | `src/downloader.py` |
| Whisper model weights | Downloaded by faster-whisper on first use | `src/transcriber.py:107` (cache location UNCONFIRMED — faster-whisper default) |
| Ollama models | Pulled by the user with `ollama pull` | `SETUP.md:21-46` |
| NLLB model | Downloaded by transformers on first use | `src/translators/nllb_backend.py:20` |
| Subtitle-gen term dictionary | External folder, optional | `config.py:13`, `src/transcriber.py:8-19` |
| Protected term lists | Built in: loanwords and capitalised terms, plus `HIGHLIGHT_EXTRA_PROTECTED_TERMS` | `src/term_protection.py:82-106` |
| Caption fonts | Committed files | `static/fonts/` |
| Scene and narration text | Produced by the LLM at run time | `src/topic_segmentation.py`, `src/narration_rewrite.py` |

## 6.7 Migrations

NOT FOUND IN CODE: a migration system. There is no database schema to migrate. Format changes to JSON files are handled
by reading both shapes where needed (for example, `attempted_highlight_languages` reads legacy metadata,
`src/highlight_jobs.py`).

## 6.8 Schema diagram

See the file-relationship diagram in [03-architecture.md](03-architecture.md), section 3.6.4.

---

# 7. API and Interfaces

All routes are defined in `app.py`. The server listens on `127.0.0.1:5000` (`app.py:1237`).

**Authentication:** none. No login, no API key, no CSRF token (checked by search). The only protection is the bind
address (`app.py:1237`).

**Size limit:** 500 MB for request bodies (`app.py:79`).

**Errors:** most routes return JSON `{"error": "<message>"}` with a status code. Some download routes return plain text
for 404.

Line numbers come from the Flask review pass and were spot-checked; treat them as approximate after future edits.

## 7.1 Pages

| Method | Path | Function (line) | Returns | Notes |
|---|---|---|---|---|
| GET | `/` | `index` (`app.py:624`) | `index.html` | Home page: input, options, progress, results, history |
| GET | `/job/<job_id>` | `job_page` (`app.py:770`) | `index.html` | Same page; the browser opens the job from the URL. The job ID is not validated here. |
| GET | `/gpu-selection` | `gpu_selection_page` (`app.py:630`) | `gpu_selection.html` | GPU page. Injects deploy status (`app.py:69-78`). |

## 7.2 Job routes

### POST `/api/summarize` — start a job
- **Function:** `summarize` (`app.py:778`).
- **Input:** `multipart/form-data`.
  - `url` (text): YouTube link. Any text that does not contain `youtube.com` or `youtu.be` is passed to the local file path
    handler (`app.py:320`, `:348-349`).
  - `file` (file): video upload. Wins over `url` if both are sent (`app.py:799`, `:811`).
  - `compression` (text): `high`, `medium`, or `low`. **Not validated** (`app.py:784`).
  - `highlight_video` (text): `true` to enable highlights (`app.py:785`).
  - `highlight_languages` (text, repeatable): codes from `en`, `hi`, `kn`, `te`, `ta` (`app.py:786-790`). A non-English code turns highlights on.
  - `pipeline_runtime` (text): `local` or `runpod` (`app.py:828`).
  - `model` (text): **ignored**. The server always uses `SUMMARIZATION_MODEL` (`app.py:783`).
- **Output:** `200` with `{"job_id": "<8 hex characters>"}` (`app.py:847`). `400` with `{"error": "Invalid file format: ..."}` for a bad
  extension (`app.py:804`) or no input (`app.py:815`). `500` with `{"error": ...}` on exception (`app.py:850`).
- **Auth:** none.

Example (illustrative):

```bash
curl -F "url=https://www.youtube.com/watch?v=VIDEO_ID" \
     -F "compression=medium" -F "highlight_video=true" \
     -F "highlight_languages=en" -F "highlight_languages=hi" \
     http://127.0.0.1:5000/api/summarize
```

```json
{ "job_id": "a1b2c3d4" }
```

### GET `/api/progress/<job_id>` — poll progress
- **Function:** `progress` (`app.py:853`).
- **Output:** `200` with the progress object (`JobProgress.to_dict`, `app.py:237-251`), or `404` with `{"error": "Job not found"}` (`app.py:856`).
- **Progress object keys:** `job_id`, `status` (`processing`, `completed`, `error`), `progress` (0 to 1), `current_step`,
  `indeterminate`, `stage_elapsed_sec`, `progress_source` (`local` or `runpod`), `error`, `error_code`, `log_lines`
  (last 20 lines, `app.py:245`).

Example response (illustrative):

```json
{
  "job_id": "a1b2c3d4",
  "status": "processing",
  "progress": 0.92,
  "current_step": "highlight_assembly",
  "indeterminate": true,
  "stage_elapsed_sec": 42,
  "progress_source": "runpod",
  "error": null,
  "error_code": null,
  "log_lines": ["[a1b2c3d4] Step: highlight_assembly (92%)"]
}
```

### GET `/api/results/<job_id>` — results
- **Function:** `results` (`app.py:876`).
- **Output:**
  - `200` with the results dictionary when completed (`app.py:882`), or when a finished job is found on disk (`app.py:898-918`).
  - `202` with `{"status": "processing"}` while the job runs (`app.py:887`).
  - `422` for a failed job whose error code is user-actionable (for example `YOUTUBE_AUTH_REQUIRED`) (`app.py:884-896`).
  - `500` for other failed jobs (`app.py:884-896`).
  - `404` if nothing is found on disk (`app.py:923`).
- **Note:** the job ID is not checked with the 8-hex pattern on this route (`app.py:876-923`).

### GET `/api/history` — list past jobs
- **Function:** `history` (`app.py:926`).
- **Output:** `200` with `{"jobs": [...newest first...], "total": <n>}` (`app.py:929-932`). Each entry is built from the job's manifest in S3 (`app.py`, `_history_entry`). Jobs still running on this machine are listed as `processing`.

### POST `/api/highlight/retry/<job_id>` — retry failed highlight languages
- **Function:** `retry_highlight` (`app.py:1018`).
- **Input:** JSON `{ "languages": [...], "model": "...", "compression": "..." }` (`app.py:1038-1044`). Not validated beyond the language list.
- **Output:** `200` `{ok, highlight_language_status, highlight_languages, highlight_video}` (`app.py:1054-1061`).
  `404` if the job ID is invalid (`app.py:1022`) or the folder is missing (`app.py:1035`).
  `409` with `retry_unavailable_runpod` when the job ran on RunPod (`app.py:1025-1032`). `500` on exception.

### GET `/api/highlight-url/<job_id>/<lang_code>` — fresh playback link
- **Function:** `api_highlight_url` (`app.py:1086`).
- **Query:** `attachment=1` for a download link (`app.py:1098`).
- **Output:** `200` JSON with a short-lived URL (`app.py:1108`). `400` for an invalid language, `404` for an invalid job ID or folder.
  `503` with `s3_unavailable` if S3 fails. Its listing is cached for 30 seconds. All responses have `Cache-Control: no-store`.
- **Lifetime:** one hour for S3 links (`src/s3_video_store.py:31`, `HIGHLIGHT_URL_EXPIRES`).

### GET `/stream/highlight/<job_id>` and `/stream/highlight/<job_id>/<lang_code>` — play a highlight
- **Function:** `stream_highlight` (`app.py:1128`). Supports HTTP range requests (`app.py:1148`).
- **Output:** `302` to a fresh one-hour S3 link for the file (`app.py`, `_file_redirect`). `404` for an invalid job ID or a file the job does not have. The local folder is never served.

### GET `/download/highlight/<job_id>` and `/download/highlight/<job_id>/<lang_code>` — download a highlight
- **Function:** `download_highlight` (`app.py:1160`). Same logic as streaming, as an attachment (`app.py:1169-1180`).

## 7.3 Download routes

| Method | Path | Function (line) | Returns | Notes |
|---|---|---|---|---|
| GET | `/download/summary/<job_id>` | `download_summary` (`app.py:936`) | `summary.txt` attachment | Generates `summary_<lang>.txt` on demand for a language (`app.py:940-952`). `400` "Invalid language". The job ID is not validated (`app.py:936-1008`). |
| GET | `/download/summary/<job_id>/<lang_code>` | same | Translated summary attachment | Language must be in `LOCALES` (`app.py:940`). |
| GET | `/download/subtitles/<job_id>` | `download_subtitles` (`app.py:967`) | `summary.srt` attachment | No link to this route in the front end (review pass: UNCONFIRMED whether used). The job ID is not validated. |
| GET | `/download/audio/<job_id>` | `download_audio` (`app.py:989`) | `audio_explanation.mp3` attachment | The job ID is not validated. |

**Path-validation gap:** the summary, subtitle and audio download routes build the path from `<job_id>` without the 8-hex
check that the highlight routes use (`app.py:1021`, `:1092`, `:1131`, `:1163`). Flask's default `<job_id>` segment cannot
contain `/`, but nothing rejects other values such as `..`, so the route does not confine reads to `OUTPUT_DIR` by itself.
Whether a crafted value can reach a file outside the folder was NOT tested. See [07](07-setup-security-testing.md).

## 7.4 GPU and deploy routes

| Method | Path | Function (line) | Input | Output |
|---|---|---|---|---|
| GET | `/api/compute-deploy-status` | `compute_deploy_status_api` (`app.py:637`) | none | Deploy status JSON, always `200` |
| GET, POST | `/api/pipeline-runtime` | `pipeline_runtime_api` (`app.py:643`) | POST: `pipeline_runtime` (JSON or form) | `200`, or `400` for an invalid value (`app.py:650-653`) |
| POST | `/api/compute-deploy-stop` | `compute_deploy_stop_api` (`app.py:657`) | none | `200` or `400`; invalidates the status cache (`app.py:663`) |
| POST | `/api/compute-network-volume-delete` | `compute_network_volume_delete_api` (`app.py:671`) | JSON or form `confirm` (`app.py:674`) | `200` or `400` |
| GET | `/api/gpu-selection/serverless-region` | `gpu_selection_serverless_region_api` (`app.py:687`) | query `data_center_id` | `400` if the ID or the API key is missing (`app.py:696-699`); `200` with rendered partials, or `502` (`app.py:726`) |
| GET, POST | `/api/gpu-selection` | `gpu_selection_api` (`app.py:731`) | POST JSON: `selection_id`, `offer`, `serverless_models`, `serverless_data_center_id`, `docker_hub_user` (`app.py:735-746`) | `200` report, or `400` (`app.py:766-767`) |

## 7.5 Example requests for the simplest routes

```bash
# Start a job from an uploaded file
curl -F "file=@lecture.mp4" -F "compression=high" http://127.0.0.1:5000/api/summarize

# Poll it
curl http://127.0.0.1:5000/api/progress/a1b2c3d4

# Fetch results once completed
curl http://127.0.0.1:5000/api/results/a1b2c3d4

# Download the summary
curl -OJ http://127.0.0.1:5000/download/summary/a1b2c3d4

# Retry two failed highlight languages
curl -H "Content-Type: application/json" \
     -d '{"languages":["hi","kn"]}' \
     http://127.0.0.1:5000/api/highlight/retry/a1b2c3d4
```

## 7.6 Interfaces that are not HTTP

| Interface | How used | Where |
|---|---|---|
| Command line `main.py` | `python3 main.py <URL or file> [--quality] [--model] [--compression] [--highlight-video] [--languages]` | `main.py:174`, arguments near `main.py:182-196` |
| Command line `download_transcript.py` | Writes a transcript from a YouTube link or file | `download_transcript.py:224` (`download_transcript`) |
| Worker job interface | `POST https://api.runpod.ai/v2/<endpoint>/run` with `{"input": {"phase": ...}}` | `runpod_worker/handler.py:202-216`; job types in [03-architecture.md](03-architecture.md) |
| Python module functions | Each `src/` module exposes functions called by `app.py` and `main.py` | [06-logic-and-structure.md](06-logic-and-structure.md), section 9 |
| Browser-side functions | Plain global functions in `static/js/app.js` | `static/js/app.js` |

## 7.7 Worker job types (RunPod)

| `input.job_type` or `input.phase` | Work | Source |
|---|---|---|
| `heavy_phase` (default) | Whisper, summary, summary translation | `runpod_worker/handler.py:61-90`, `src/runpod_heavy_core.py:18-68` |
| `highlight_phase` | Highlight pipeline on the worker | `runpod_worker/handler.py:150-188` |
| `nllb_batch` | Batch NLLB translation | `runpod_worker/handler.py:191-199` |
| `warmup` | Loads models so the first real job is fast | `runpod_worker/handler.py:101-117` |
| `diagnostic` | Reports library versions and CUDA presence, loads no models | `runpod_worker/handler.py:120-147` |

Unknown types raise an error (`runpod_worker/handler.py:216`).

## 7.8 Outbound interfaces (what the app calls)

The full list is in [03-architecture.md](03-architecture.md) and [04-tech-stack.md](04-tech-stack.md). The RunPod
management endpoints used are:

| Method | URL | Purpose | Source |
|---|---|---|---|
| POST | `https://api.runpod.io/v2/serverless` | Create endpoint | `src/runpod_serverless.py:215` |
| GET | `https://api.runpod.io/v2/serverless/{id}` | Read endpoint | `src/runpod_serverless.py:226` |
| PATCH | `https://api.runpod.io/v2/serverless/{id}` | Change workers, detach volume | `src/runpod_serverless.py:239`, `:374-377` |
| POST | `https://api.runpod.ai/v2/{id}/run` | Submit a job | `src/runpod_serverless.py:431-437` |
| GET | `https://api.runpod.ai/v2/{id}/status/{job}` | Poll a job | `src/runpod_serverless.py:469-479` |
| GET | `https://api.runpod.ai/v2/{id}/health` | Worker and job counts | `src/runpod_serverless.py:458-466` |

The `api.runpod.io/graphql` calls exist only in two scripts outside the main code path (`scripts/deploy_v104_timed.py`,
`scripts/deploy_v105_timed.py`).
