# 6. Core Logic, Algorithms and Folder Structure

[← Data and API](05-data-and-api.md) · [Index](../PROJECT_DOCUMENTATION.md) · Next: [Setup, Security, Testing →](07-setup-security-testing.md)

Sections 8 (logic) and 9 (structure) of the documentation brief are here.

---

# 8. Core Logic and Algorithms

Each algorithm is written as numbered steps in plain language, with the code that does it.

## 8.1 The job pipeline (`process_video`)

**Everyday analogy:** an assembly line with labelled stations. Each station posts its name and percentage on a board, so
the person watching knows where the job is.

Steps and the progress value each one reports (`app.py:283-614`):

1. **Download** — `downloading` at 5%, then 10% (`app.py:318`, `:351`). YouTube via `src/downloader.py`, or the local file.
2. **Extract audio** — `extracting_audio` at 15%, then 25% (`app.py:356`, `:359`). FFmpeg writes `audio.mp3`.
3. **Transcribe** — `transcribing` at 30%, moving toward 60% while transcription runs (`app.py:362`, `:412`). On RunPod the
   value is indeterminate (`app.py:412`).
4. **Summarize** — `summarizing` at 85% (`app.py:425`).
5. **Narration** — `generating_audio` at 90% (`app.py:452`).
6. **Subtitles** — `generating_subtitles` at 95% then 98% (`app.py:470`, `:479`, `:482`).
7. **Highlights** (if requested) — `highlight_assembly` at 92%, indeterminate (`app.py:493`); `highlight_complete` at 98%
   (`app.py:525`).
8. **Complete** — 100%, only when the job is marked complete (`app.py:530`). Results are written, the history entry is
   updated, and `summary_metadata.json` is written (`app.py:552-571`).
9. **Failure** — the job is marked failed with a sanitised message and a code (`app.py:578-604`).

Rules:

- **Progress never goes down.** `set_progress` only moves forward (`app.py:222`).
- **100% only at the end.** `set_step` caps at 99% (`app.py:193`); only `mark_completed` sets 100% (`app.py:201`).
- **The checklist follows pipeline order, not percentages** (`static/js/app.js`, `subtaskStates`). Earlier steps are
  completed, the current step is active, later steps are pending. A high-water mark stops a late report from moving it back.

## 8.2 Transcription and term correction (`src/transcriber.py`)

1. Load the Whisper model `base` on CPU with `compute_type="auto"` (`src/transcriber.py:107`; the CLI passes `base`,
   `main.py:66`).
2. **Pass 1:** transcribe in English. Each segment is reported as it finishes, so progress is measured
   (`src/transcriber.py:126-133`). `condition_on_previous_text=False` stops errors from one segment carrying into the next.
3. **Pass 2:** for each word of two or more letters (`src/transcriber.py:50-85`):
   - if an exact correction exists, replace it;
   - else, if the word looks suspicious, try a fuzzy match with a similarity threshold of 75 (`src/transcriber.py:27-42`);
   - else, record it as uncertain.
4. A word is **suspicious** if it is all capitals of 2 to 6 letters (except `api` and `url`), has a lower-to-upper case
   change inside it, or mixes letters and digits (`src/transcriber.py:27-42`).
5. Write `transcript.txt`, `segments.json`, and `uncertain_terms.json` if any words were uncertain (`src/transcriber.py:147-158`).

**Known weakness:** the replacement is a plain substring replacement with no word boundaries (`src/transcriber.py:44-48`),
so a short correction can change part of a longer word.

**Everyday analogy:** a spell-checker with a personal dictionary. Known words are fixed automatically; odd-looking words
are either matched to the closest dictionary word or put on a list for a human to check.

## 8.3 Summarization (`src/summarizer.py`)

**Everyday analogy:** a teacher who first makes a list of every idea in the lecture, marks each idea as essential,
important or supporting, keeps only the share the student asked for, and then writes the study sheet from that list.

1. **Chunk:** split the transcript on sentence boundaries into chunks of at most 10,000 characters (`src/summarizer.py:79`,
   `:578`). A single very long sentence is kept whole.
2. **Pass 1 — extract and rank:** for each chunk, ask the model for concept lines tagged `[CRITICAL]`, `[IMPORTANT]` or
   `[SUPPORTING]`. Options: temperature 0.3, top-p 0.9, context 8192 (`src/summarizer.py:171-239`). If no tags are parsed,
   retry once with a format reminder; if still none, raise.
3. **Pass 2 — select:** keep a share of all tagged concepts (`src/summarizer.py:242-290`):
   - `high` keeps 20%,
   - `medium` keeps 60%,
   - `low` keeps 100% (`src/summarizer.py:265-267`).
   All CRITICAL concepts are always kept, then IMPORTANT, then SUPPORTING fill the rest. At least one concept is kept.
4. **Pass 3 — write:** each chunk is written using only its own selected concepts. Chunks with no selected concepts are
   skipped. The chunk summaries are joined with blank lines and **not** summarised again (`src/summarizer.py:598-633`).
5. **Link to time:** each summary sentence longer than 10 characters is matched to a transcript segment by its first five
   words (`src/summarizer.py:484-534`). A match scores 0.9. A sentence with no match is given the start of segment 0 and a
   score of 0.5.

**Known weaknesses (from the review pass, `src/summarizer.py:484-534`):**
- Every unmatched sentence points at the start of the video, so the timestamps for those sentences are misleading.
- The word-target prompts in `src/summarizer.py:376-417` (about half, 70% and 85% of the words) run only when no concept
  list is given, which the pipeline never does. Their docstrings say 50/70/85%, which does not match the 20/60/100% concept
  selection. In practice, compression means concept selection.
- The default compression is `high` in the UI (`templates/index.html:97`) and `medium` in the function (`src/summarizer.py:242`).

## 8.4 Scene planning for highlights (`src/topic_segmentation.py`, `src/highlight_chunking.py`)

**Everyday analogy:** a film editor who reads the script and marks where the scene changes, then picks one picture for
each scene.

1. **Chunk the transcript** into groups of whole segments of at most 10,000 characters (`src/highlight_chunking.py:27-76`).
   Each line is labelled with its segment number.
2. **Ask the LLM** (`src/topic_segmentation.py:46-94`) for natural topic changes, one to eight per chunk, each giving:
   - the segment where the topic starts,
   - a short title,
   - a one-to-three-sentence summary.
   Options: temperature 0, seed 42, context 8192, JSON output.
3. **Validate** (`src/topic_segmentation.py:97-189`):
   - the segment number must be an integer inside the chunk;
   - numbers must strictly increase (duplicates and out-of-order values are dropped);
   - empty summaries are dropped; empty titles become "Topic at segment N";
   - zero valid topics is an error.
4. **Retry** once per chunk (two attempts in total, `src/topic_segmentation.py:192-217`).
5. **Fallback:** if any chunk fails after the retry, or fewer than two valid scenes remain after joining chunks, the whole
   plan is replaced by evenly spaced scenes (`src/topic_segmentation.py:267-268`, `:329-351`). The count is between 4 and 12,
   about one per minute of video.
6. **Merge** scenes that start less than 40 seconds apart (`src/topic_segmentation.py:228-253`, `config.py:49`).

**Scene count:** the LLM decides the count. Compression does **not** change the scene count (see
[08](08-history-limits-results.md), section 15). The `max_scenes` option exists but only scripts pass it.

**Known risk:** the fallback summary text ("Summary unavailable; evenly spaced fallback scene.") is passed to narration
rewriting without a check, so it may be spoken aloud (`src/highlight_pipeline.py:137-144`, UNCONFIRMED at run time).

## 8.5 Visual choice per scene (`src/visual_extractor.py`)

**Everyday analogy:** a photographer who takes five quick shots, keeps the sharpest, and only uses a short video clip if
the scene is moving a lot.

1. **Window:** look at video time from 2.0 to 5.0 seconds after the scene start (`src/visual_extractor.py:64-68`;
   `config.py:50-51`).
2. **Motion score:** take three frames at 320×180 in grey; the score is the average pixel difference between neighbouring
   frames (`src/visual_extractor.py:103-131`).
3. **Sharpness:** grab five candidate frames; for each, compute the variance of a Laplacian filter over the full frame
   (`src/visual_extractor.py:89-100`). Keep the sharpest.
4. **Decide** (`src/visual_extractor.py:229-303`):
   - If the motion score is above 35.0 (`config.py:56`) **and** the sharpest frame scores at least 35.0 (`config.py:53`),
     use a **clip**: a 4-second muted video centred on the scene time (`config.py:94`).
   - Otherwise, if the sharpest frame scores at least 35.0, use a **still** JPG.
   - Otherwise, use a **placeholder** PNG with the scene title.
5. **Manifest:** record the mode, time and scores per scene in `visual_manifest.json` (`src/visual_extractor.py:327-350`).

Note: the same threshold value (35.0) is used for two different measures (motion and sharpness); the constants are separate.

## 8.6 Narration rewrite (`src/narration_rewrite.py`)

1. **Word limit by compression:** `high` 30 words, `medium` 45, `low` 70 (`config.py:100-104`). Unknown values use `medium`.
2. **Ask the LLM** to rewrite the summary as two or three plain sentences with no new facts, within the limit
   (`src/narration_rewrite.py:65-98`). Context 4096, temperature 0, seed 42.
3. **Enforce the limit:** if too long, ask once to shorten; if still too long, cut at a sentence boundary; if still too long,
   cut at the word limit (`src/narration_rewrite.py:101-126`).
4. **Failure:** if the model is missing or returns nothing, use the trimmed summary (`src/narration_rewrite.py:129-155`).

## 8.7 Timing and the shared video (`src/highlight_shared_video.py`, `src/highlight_timing.py`)

1. Generate English speech for each scene to measure how long it takes (`src/highlight_shared_video.py:36-83`).
2. **Display length** of each scene = the longer of the narration length and 5.5 seconds (`src/highlight_timing.py:10`,
   `config.py:55`).
3. Encode each scene's muted picture **once** into `highlights/_shared/scene_NNN/visual.mp4` (`src/highlight_shared_video.py:44`).
4. Save the timings to `highlights/_shared/scene_timing.json`.

**Consequence:** every language uses the English timing. A longer translation is cut at the scene length
(`src/highlight_assembler.py:57-59`).

## 8.8 Per-language rendering (`src/highlight_pipeline.py:198-303`, `:366-401`)

1. **Translate** the scene text. Up to four languages run in parallel (`src/highlight_pipeline.py:368`).
2. For each scene, **speak** the translated text with the language's voice, falling back as described in 8.9.
3. **Caption:** build an ASS subtitle file for each scene (`src/highlight_captions.py`):
   - word-by-word captions when word timings exist (non-Indic scripts);
   - for Indic scripts with word timings, grouped to about 18 characters per line;
   - without word timings, seven words per line spread evenly over the narration length (`src/highlight_captions.py:87-136`).
4. **Mux:** combine the shared picture with the narration, scale to 1280×720 at 30 fps, and burn in the subtitles
   (`src/highlight_assembler.py:213-253`). The source video's sound is never used.
5. **Concatenate** the scenes into `highlight_<lang>.mp4` (`src/highlight_assembler.py:303-333`).
6. **Record** the result in `language_status.json` and merge it into `highlight_status.json`.

**Failure isolation:** a translation or render error marks only that language as failed (`src/highlight_pipeline.py:387-399`,
`:441-456`). A shared-stage error (English timing or visuals) stops the whole highlight phase without per-language
records (`src/highlight_pipeline.py:408-413`; review pass).

## 8.9 Narration voices and fallback (`src/highlight_tts.py`, `src/locales.py`)

Voice chain for each language, tried in order:

| Language | Primary voice | Fallback voice | Caption font file | gTTS language |
|---|---|---|---|---|
| English (`en`) | `en-US-GuyNeural` (`config.py:62`) | `en-IN-NeerjaNeural` | `static/fonts/DejaVuSans.ttf` (family "DejaVu Sans") | `en` |
| Hindi (`hi`) | `hi-IN-SwaraNeural` | `hi-IN-MadhurNeural` | `NotoSansDevanagari-Regular.ttf` | `hi` |
| Kannada (`kn`) | `kn-IN-SapnaNeural` | `kn-IN-GaganNeural` | `NotoSansKannada-Regular.ttf` | `kn` |
| Telugu (`te`) | `te-IN-ShrutiNeural` | `te-IN-MohanNeural` | `NotoSansTelugu-Regular.ttf` | `te` |
| Tamil (`ta`) | `ta-IN-PallaviNeural` | `ta-IN-ValluvarNeural` | `NotoSansTamil-Regular.ttf` | `ta` |

Source: `src/locales.py` (locale blocks), voices verified in the handoff notes (`docs/AI_HANDOFF_CONTEXT.md`, section 4.2).

1. Try the primary voice with Edge TTS at speaking rate "-10%" (`src/highlight_tts.py:42-47`).
2. Try the fallback voice.
3. Use gTTS (`src/highlight_tts.py:118-130`).
4. If scene audio is longer than 20 seconds, log a warning only (`src/highlight_tts.py:106-111`).

## 8.10 Translation chain (`src/highlight_translate.py`, `src/translators/`)

**Everyday analogy:** asking a bilingual friend first; if they are busy, asking a dictionary app; and if that fails, a
language model.

Default order: **Google → NLLB → Ollama** (`src/translators/translation_chain.py:9-27`). Any backend that fails hands over
to the next (`src/highlight_translate.py:280-307`).

Google (`src/translators/google_backend.py`):
1. **Throttle:** at least `max(HIGHLIGHT_GOOGLE_REQUEST_DELAY_SEC, 1.0)` seconds between requests, shared across the
   process (`src/translators/google_backend.py:55-68`).
2. **Retries:** other errors are retried up to five times with waits of 2, 4, 8 and 16 seconds (`:27-28`, `:212-219`).
3. **Rate limit:** an HTTP 429 or "too many requests" text is retried up to four times, waiting 5 seconds times the streak
   (`:29-30`, `:199-209`). After that the Google circuit breaker trips.
4. **Circuit breaker** (`src/translators/google_circuit.py:1-45`): once tripped for a job, every later Google call in that
   job fails at once, and that language skips Google (`src/highlight_translate.py:284-289`).

Per scene (`src/translation.py:53-116`): two attempts. A rate-limit failure stops at once. The result must contain at least
one letter in the target script (`src/locales.py:125-135`), otherwise it is rejected.

**Cache** (`src/translation_cache.py:12-14`): key = language, backend, and the first 20 hex characters of the SHA-256 of the
trimmed text. Stored in `highlights/_shared/translation_cache.json`. Cached values are the final text, after protection
(`src/translators/factory.py:52-56`).

**Term protection** (`src/term_protection.py:82-153`):
- text inside backticks is not translated;
- protected terms are capitalised words of three or more letters, all-capital tokens, a loanword list (for example "python",
  "api", "cpu", "sushi"), and anything in `HIGHLIGHT_EXTRA_PROTECTED_TERMS`;
- each protected term goes through the glossary, then Indic transliteration (ITRANS), and otherwise stays as it is
  (`src/simple_indic_translit.py:32-62`).

**Quality gate** (`src/translation_quality.py:207-243`): at least eight target-script letters; at most two letters from another
Indic script; no leftover JSON; no run of six or more identical words; no word repeated forty or more times. A failure is an error.

**NLLB** (`src/translators/nllb_backend.py`): `facebook/nllb-200-distilled-600M`, on CUDA when available. Output length is
between 256 and 1536 tokens, about 2.5 times the input (`:72`). With `NLLB_VIA_RUNPOD=1` it calls the RunPod proxy (`:58-61`).

**Ollama** (`src/translators/ollama_backend.py`): expects a JSON object; if the reply is short and not JSON, the raw text is used
(`:82-100`). Temperature 0, seed 42, context 4096.

**Summary translation** (`src/summary_translate.py`): splits the summary at `###` headings with a 1200-character limit
(`:55-77`) and uses only Google and NLLB, not Ollama (`:168-170`).

## 8.11 YouTube download and error classification (`src/downloader.py`, `src/youtube_errors.py`)

1. **Client profiles,** tried in order (`src/downloader.py:25-30`): android; tv then android; android then web-embedded; iOS.
   The metadata fetch always uses android (`:72`, `:82`).
2. **Format:** the best video at or below the quality, merged with the best audio, as MP4
   (`src/downloader.py:107-111`). Default quality 360 from the CLI (`main.py:186`).
3. **Attempts:** up to three; each attempt tries all four client profiles (`:149`, `:212-255`). Waits of 2 and 4 seconds.
   Non-retryable errors stop at once.
4. **Classification** (`src/youtube_errors.py:14-19`, `:53-77`), with precedence AUTH, then PRIVATE, then UNAVAILABLE over
   transient errors (`:127-142`):

| Code | Meaning | Retry? |
|---|---|---|
| `YOUTUBE_AUTH_REQUIRED` | Sign-in, age gate, members-only, or bot check | No — upload the file instead |
| `YOUTUBE_PRIVATE` | Private video | No |
| `YOUTUBE_UNAVAILABLE` | Removed, terminated, or blocked in the region | No |
| `YOUTUBE_RATE_LIMITED` | HTTP 429 | Yes |
| `YOUTUBE_NETWORK` | Timeout, reset or DNS failure | Yes |
| `YOUTUBE_DOWNLOAD_FAILED` | Anything else | Yes |

**Known defect:** a leftover `.mkv` or `.webm` file is renamed to `.mp4` without re-muxing (`src/downloader.py:160-165`).

## 8.12 Subtitle file (`src/subtitle_generator.py`)

1. One cue per linked summary sentence, numbered from 1 (`src/subtitle_generator.py:63`).
2. Timing comes from the transcript-based links in 8.3, **not** from the narration audio.
3. Text is cleaned: markdown removed, bullets turned into sentences, symbols spoken (`src/subtitle_generator.py:22-60`).
4. Format `HH:MM:SS,mmm --> HH:MM:SS,mmm`, text, blank line (`src/subtitle_generator.py:13-19`).

**Known defect:** milliseconds are rounded, so a value just under a second can print as `1000` (`src/subtitle_generator.py:18`).

## 8.13 Narration text for the summary path (`src/tts_generator.py`, `src/text_cleaner.py`, `src/hierarchy_detector.py`)

1. Clean the text into speech-friendly sentences, recording heading levels (`src/text_cleaner.py:148`).
2. Add pauses by heading level: 1000 ms (top), 800 ms (second), 300 ms (deeper); speaking rate 85%, 100%, 110%
   (`src/hierarchy_detector.py:101-138`).
3. Speak with `en-US-GuyNeural` at rate "-10%" (`src/tts_generator.py:75-79`). The locale voice chain is **not** used here.
4. **Fallbacks:** if Edge TTS fails for any reason other than a missing package, use gTTS (`src/tts_generator.py:219-237`).
   If gTTS also fails, an **empty** file is written and success is reported (`src/tts_generator.py:239-243`).

**Known defects:**
- A missing `edge_tts` package triggers a `pip install` and an error, so gTTS is never reached (`src/tts_generator.py:203-210`).
- Rate markers are removed before Edge TTS, so they have no effect (`src/tts_generator.py:60-69`).

## 8.14 The RunPod serverless job (`src/runpod_serverless.py`, `src/runpod_serverless_payload.py`)

1. **Wake the endpoint if paused:** if its maximum worker count is zero, raise it to min 0, max 3, idle 5 seconds
   (`src/runpod_serverless.py:268-278`). If the status read fails, submit anyway (`:271-272`).
2. **Submit** with `POST /v2/{id}/run` (`src/runpod_serverless.py:431-437`):
   - HTTP 409 "paused" → wait 10 seconds and retry, for up to 240 seconds (`:264-265`, `:281-317`);
   - HTTP 502, 503, 504 → **no retry**, because the job might already be queued (`src/runpod_serverless.py`, `submit_job`);
   - other errors → fail at once.
3. **Poll** `GET /v2/{id}/status/{job}` every 2 seconds for up to 7200 seconds (`src/runpod_serverless.py:482-514`).
   Terminal states: COMPLETED, FAILED, CANCELLED, TIMED_OUT (`:39`). Anything other than COMPLETED is an error.
4. **Lease:** the capacity lease is held from submit until the poll finishes (`src/runpod_serverless.py:320-342`).

**Payload size ladder** (`src/runpod_serverless_payload.py:12-14`; the body limit is about 10 MB, the safe target 9 MB):
- **Audio** (always inline): re-encode to mono 16 kHz MP3 at 64, 48, 32, 24, 16 and 12 kbps until it fits; otherwise fail with
  "Try a shorter video or split the lecture" (`:78-127`).
- **Video** (inline highlight path): re-encode to 480, 360, then 240 pixels high, with CRF 28 to 40 (`:343-383`); otherwise
  fail, with no local render.
- **Video with S3:** when `VIDEO_S3_WORKER_URL_SUPPORT` is on, no video bytes are sent; the worker downloads the source with a signed link (`src/runpod_serverless_payload.py`).

**Worker job routing:** `input.job_type` or `input.phase` selects the work (`runpod_worker/handler.py:202-216`); see
[05](05-data-and-api.md), section 7.7.

## 8.15 Capacity leases (`src/endpoint_capacity.py`)

**Rule:** the endpoint's maximum worker count stays at zero unless a valid lease exists (`src/endpoint_capacity.py:3-4`).

1. **Take a lease** before submitting a job; write it as a JSON file under `data/endpoint_leases/` with a file lock and a
   60-second lock timeout (`:36`, `:42`, `:71`).
2. **Release** the lease when the job finishes, fails or times out (`:151-180`). If no leases remain, set the maximum to zero
   and read it back to confirm (`:111-132`, `:164-170`).
3. **Expiry:** a lease older than four hours is ignored (`:37`, `:98-108`).
4. **Sweep:** on start-up (`app.py:1231`, `src/endpoint_capacity.py:267-280`) and every 300 seconds in the background
   (`app.py:1232`, `:41`, `:242-264`).

**Known gaps** (review pass): "Stop workers" and "Delete volume" set the maximum to zero without checking leases
(`src/compute_deploy.py:479-504`, `:530-534`), so an in-flight job can be affected (UNCONFIRMED); jobs running longer than four
hours lose their lease (`src/endpoint_capacity.py:13-17`).

## 8.16 Live status states (`src/serverless_live_status.py`)

| State | When | Source |
|---|---|---|
| `not_deployed` | No endpoint saved | `:78-80` |
| `unavailable` | Settings or health read failed | `:84-105` |
| `queued_max_zero` | Queue > 0, idle 0, running 0, max 0 (labelled inference) | `:110-116` |
| `waiting_for_capacity` | Same, but max > 0 (labelled inference) | `:117-121` |
| `running` | Running > 0 (not proof a video is processing) | `:122-126` |
| `workers_idle_only` | Idle > 0, running 0 | `:127-131` |
| `idle` | Otherwise | `:132-135` |

Results are cached for 5 seconds; failed reads are not cached; concurrent requests share one refresh
(`src/serverless_status_cache.py:15-72`).

## 8.17 S3 storage rules (`src/s3_video_store.py`, `src/pipeline_executor.py`, `app.py`)

1. **Required:** `ensure_ready()` checks credentials, bucket access, and that credentials last long enough for one-hour
   links. `/api/summarize` runs it before anything is saved or started (`app.py`, `summarize`).
2. **Source video:** uploaded to `jobs/<job_id>/<video_uid>/source.mp4` before processing, with SSE-S3 encryption, and
   recorded in the job's file list. It is kept, not deleted after the job.
3. **Every other file** in the job folder is uploaded at the end (`publish_job`), except local bookkeeping and render
   scratch (`s3_video.json`, `s3_highlights.json`, `build/` folders). Each upload is checked by size.
4. **Manifest last:** `manifest.json` holds the job ID, video ID, status, results, and each file's key. A job is listed in
   History only when its manifest exists. A failed upload writes no manifest.
5. **Failed jobs:** `publish_failure` writes a manifest with `status: error`, the neutral message, and the code, so
   History shows the failure and the job can be deleted.
6. **History and reads:** History and results come from manifests in S3 (30-second cache, cleared by any write). Downloads
   and playback redirect to a fresh one-hour presigned link for the file's own key. Nothing is served from the local folder.
7. **Retries:** the job's files are copied from S3 to a temporary folder, the failed languages are rendered, and only
   changed files are uploaded again (`_file_digests`).
8. **Delete:** `delete_job` removes every file, then the manifest last. A partly failed delete keeps the manifest.
9. **Links:** the RunPod worker gets a presigned source link only when `VIDEO_S3_WORKER_URL_SUPPORT` is true. Links are
   never logged or saved.

**Gap:** no bucket lifecycle rule exists in code, so storage grows until jobs are deleted.
`docs/S3_SOURCE_RETENTION_PLAN.md` describes a proposal, not an implementation.

## 8.18 Logging safety (`src/log_safety.py`)

Pattern-based redaction (`redact`, `src/log_safety.py:54`) removes:
- query strings from signed URLs, leaving the host and path;
- user name and password in URLs;
- bearer tokens;
- JSON or key=value pairs whose key looks secret (token, password, signature, cookie, and similar);
- AWS access key IDs;
- long token-like strings;
- cookie flags such as `--cookies`.

Limit (stated in the module): a secret with no recognisable label or shape is not caught.

---

# 9. Folder and Code Structure

Annotated tree of the repository (tracked files; generated folders are marked).

```
video-summarizer/
├── app.py                    Flask web app: routes, job threads, progress, results, history (1,237 lines)
├── main.py                   Command-line full pipeline (the entry point for terminal use)
├── config.py                 Settings: model names, folders, highlight tuning constants
├── download_transcript.py    Standalone: YouTube or file → transcript only (no Ollama)
├── requirements.txt          Loose dependency list for quick installs
├── requirements-nllb.txt     Optional NLLB extras (not pinned; see docs)
├── requirements/             Input (.in) and locked (.lock) dependency files per environment
│   ├── common.in, local.in, mac-cpu.in, windows-cpu.in, worker.in
│   ├── mac-cpu.lock          Hashed pins for macOS (used by CI and the local launcher)
│   ├── windows-cpu.lock      Hashed pins for Windows
│   ├── worker.lock           Hashed pins for the GPU worker image (no torch; base image provides it)
│   ├── worker-gtts.lock      gTTS pin, installed with --no-deps
│   ├── worker-constraints.txt, worker-base-constraints.txt   Version limits for the worker build
├── src/                      All application logic (about 73 modules)
│   ├── downloader.py          YouTube download with client fallbacks
│   ├── youtube_errors.py      YouTube error codes and messages
│   ├── job_video.py           Copies the source video into the job folder
│   ├── audio_extractor.py     FFmpeg: video → audio.mp3
│   ├── transcriber.py         faster-whisper transcription and term correction
│   ├── summarizer.py          Ollama multi-pass summary, timestamp linking
│   ├── summary_jobs.py        Read-only helpers for summary translations (used by app.py)
│   ├── summary_translate.py   Summary translation into other languages
│   ├── translation.py         Per-scene translation with retry and abort
│   ├── translation_cache.py   Cache file for translations
│   ├── translation_log.py     Log of protected terms and failures
│   ├── translation_quality.py Quality gate for translated text
│   ├── term_protection.py     Keeps names and loanwords unchanged during translation
│   ├── simple_indic_translit.py  Transliteration helper for Indic scripts
│   ├── text_cleaner.py        Markdown to speech-friendly text
│   ├── hierarchy_detector.py  Heading levels, pauses and speaking rates
│   ├── natural_speech_generator.py  Speech helper (imported but not used on the main TTS path)
│   ├── tts_generator.py       Summary narration: Edge TTS, gTTS fallback
│   ├── subtitle_generator.py  SRT subtitle file
│   ├── domain_handler.py      Pronunciation hints for code, maths and chemistry terms
│   ├── topic_segmentation.py  Highlight scene planning with the LLM
│   ├── highlight_pipeline.py  Highlight orchestration (the main highlight file)
│   ├── highlight_chunking.py  Groups segments for the planner
│   ├── highlight_timing.py    Scene display lengths
│   ├── highlight_llm.py       Deterministic Ollama options
│   ├── highlight_tts.py       Scene narration with fallback voices
│   ├── highlight_captions.py  ASS caption files
│   ├── highlight_assembler.py FFmpeg filter chains and concatenation
│   ├── highlight_shared_video.py  Shared muted picture per scene
│   ├── highlight_ffmpeg.py    FFmpeg discovery, libass check, path escaping
│   ├── highlight_jobs.py      Status files and derived status for the UI
│   ├── highlight_translate.py Per-language translation batches
│   ├── visual_extractor.py    Still, clip or placeholder per scene
│   ├── narration_rewrite.py   Short narration per scene
│   ├── job_progress.py        Progress reporting helpers used by the pipeline
│   ├── locales.py             Languages, voices, fonts (single registry)
│   ├── log_safety.py          Secret redaction for logs and errors
│   ├── pipeline_runtime.py    Chooses local, pod or serverless per job
│   ├── pipeline_executor.py   Runs each stage in its chosen place
│   ├── pipeline_model_status.py  Reports which models are available
│   ├── compute_deploy.py      Creates and updates RunPod endpoints and volumes
│   ├── compute_status.py      Status banner data for the GPU page
│   ├── gpu_selection.py       GPU page: catalogue, selection, deploy helpers
│   ├── gpu_recommendations.py Suggested GPU and model combinations
│   ├── serverless_gpu_region.py  Region-specific catalogue for the GPU page
│   ├── serverless_live_status.py Names the live endpoint state
│   ├── serverless_status_cache.py  5-second cache for RunPod reads
│   ├── serverless_model_catalog.py Which models run on the worker; highlight location
│   ├── serverless_worker_image_publish.py  Builds and pushes the worker image
│   ├── runpod_serverless.py   RunPod serverless: submit, poll, endpoint API
│   ├── runpod_serverless_catalog.py  GPU catalogue for serverless
│   ├── runpod_serverless_endpoint_config.py  Endpoint settings (workers, timeouts, env)
│   ├── runpod_serverless_payload.py  Payload size limits and re-encoding
│   ├── runpod_heavy_core.py   Heavy-phase logic shared by worker and pod
│   ├── runpod_http.py         Pod HTTP helper (no callers found in src/, app.py or worker)
│   ├── runpod_hub_worker_catalog.py  Hub worker catalogue
│   ├── runpod_jupyter.py      Pod transfer through the Jupyter proxy
│   ├── runpod_ssh.py          Pod transfer over SSH
│   ├── runpod_nllb_proxy.py   Sends NLLB calls to RunPod
│   ├── runpod_platform_controls.py  Notes on platform controls (informational)
│   ├── endpoint_capacity.py   Leases that keep the endpoint at zero when idle
│   ├── s3_video_store.py      Optional S3 storage and signed links
│   └── translators/           Translation backends and the chain
│       ├── factory.py          Chooses backends; wraps cache and term protection
│       ├── translation_chain.py  Default order of backends
│       ├── google_backend.py   Google Translate client, throttle, retries
│       ├── google_circuit.py   Per-job circuit breaker for Google
│       ├── google_guard.py     Blocks live Google calls in tests
│       ├── caching.py          Cache wrapper
│       ├── nllb_backend.py     NLLB model
│       ├── ollama_backend.py   Ollama translation with JSON output
│       └── fixture_backend.py  Test-only translator from a fixture file
├── templates/
│   ├── index.html            Home page: input, options, progress, results, history
│   ├── gpu_selection.html    GPU page
│   └── partials/             Pieces of the GPU page (status, catalogue, recommendations)
├── static/
│   ├── js/app.js             Home page logic: polling, results, history, highlights
│   ├── js/deploy_status.js   GPU page: status banner and actions
│   ├── css/style.css         Styles
│   ├── fonts/                Caption fonts (DejaVu, Noto Indic)
│   └── licenses/             Font licence texts
├── runpod_worker/
│   ├── Dockerfile            GPU worker image build
│   ├── handler.py            Worker entry point: job types
│   └── requirements-worker.txt  Stale loose list; not used by the Dockerfile
├── scripts/                  Helpers, smoke tests and deploy scripts (see below)
├── tests/                    32 test files, 420 test methods (see section 12)
├── docs/                     Handoff and setup notes (see index)
├── .github/workflows/tests.yml  CI: tests on macOS and Windows
├── .env.example              Template for environment variables (no values)
├── .gitignore, .dockerignore Keep secrets, outputs and caches out of Git and the image
├── README.md, SETUP.md, PROJECT_OVERVIEW.md, translations_compare.md   Documentation (some out of date; see section 15)
├── data/                     (generated, gitignored) saved selection and leases
├── output/                   (generated, gitignored) job folders
└── downloads/                (generated, gitignored) downloaded videos
```

## 9.1 `scripts/` by purpose

| Purpose | Files |
|---|---|
| Deploy to RunPod (can start paid resources) | `deploy_v103_pipeline.sh`, `deploy_v104_timed.py`, `deploy_v105_timed.py`, `diagnose_worker_v106.py`, `run_serverless_gpu_e2e.py`, `run_jupyter_gpu_e2e.py`, `bootstrap_serverless_deploy.py` |
| Runs on the worker or pod | `runpod_heavy_worker.py`, `runpod_heavy_http_server.py`, `runpod_nllb_batch.py` |
| Checks AWS (read-only) | `check_video_s3.py` |
| Local tests and checks (no paid service) | `run_text_only_regression.py`, `run_topic_segmentation_repeatability.py`, `test_highlight_full.py`, `test_highlight_stage1.py`, `test_multilang_one_scene.py`, `verify_nllb_hi_scene.py`, `validate_env.py`, `compare_translators.py`, `create_lecture_fixture.py` |
| Network checks | `flask_smoke_test.py`, `run_benchmark_b.py` (Google, optional live), `check_edge_tts_boundaries.py` (Microsoft TTS) |
| Launchers | `run_local_mac.sh`, `run_local_windows.ps1` |
| Fixtures | `fixtures/highlight_smoke_translations.json` |

The scripts `deploy_v103`–`v106` are one-off helpers from the deploy history. Their use is outside this document's scope.
