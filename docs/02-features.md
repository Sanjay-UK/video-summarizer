# 2. Features

[← Overview](01-overview.md) · [Index](../PROJECT_DOCUMENTATION.md) · Next: [Architecture →](03-architecture.md)

Each feature lists what it does, how a user reaches it, and which code implements it. "Analogy" lines are plain-language
comparisons for the harder ideas.

---

## F1. Video input: YouTube link or file upload

- **What:** the user gives a YouTube URL, or uploads a video file (MP4, MKV, AVI, MOV, FLV, WMV, WEBM).
- **How a user uses it:** on the home page, choose the "YouTube URL" or "Upload" tab, paste a link or drop a file, then
  press the start button (`templates/index.html:54-76`, `static/js/app.js:424-510`).
- **Implemented in:** `app.py` (`summarize`, `app.py:778`), `src/downloader.py` (YouTube download), `src/youtube_errors.py`
  (error classes), `templates/index.html`, `static/js/app.js`.
- **Limits:** 500 MB upload cap (`app.py:79`); extensions checked by name only (`app.py:800`).
- **Analogy:** the input desk at a library. It checks the item is one of the accepted formats before it goes in.

## F2. YouTube download with fallbacks and clear errors

- **What:** downloads the video with `yt-dlp`, trying several YouTube "client" profiles in turn (android, tv, ios,
  web_embedded) because YouTube sometimes blocks one profile (`PROJECT_OVERVIEW.md:124`, `src/downloader.py`).
- **Errors:** each failure is classified into a code such as `YOUTUBE_AUTH_REQUIRED`, `YOUTUBE_PRIVATE`,
  `YOUTUBE_UNAVAILABLE`, `YOUTUBE_RATE_LIMITED`, `YOUTUBE_NETWORK`, `YOUTUBE_DOWNLOAD_FAILED`, with a user-facing message
  (`src/youtube_errors.py`). Non-retryable codes stop the attempts at once (`src/downloader.py`).
- **Analogy:** trying several keys on the same door. If one key is refused, try another, but stop at once if the door is
  locked for good.
- **Redaction:** download error text is passed through the same redaction as the rest of the logs, including cookie
  flags (`src/log_safety.py`, F24).

## F3. Audio extraction

- **What:** FFmpeg pulls the audio track out as `audio.mp3` in the job folder (`src/audio_extractor.py`).
- **Implemented in:** `src/audio_extractor.py`; called from `app.py` (`process_video`, `app.py:283`).

## F4. Transcription with timestamps and term correction

- **What:** converts speech to text with timestamps, using faster-whisper. Writes `transcript.txt` and `segments.json`
  (`src/transcriber.py`).
- **Two passes:** pass 1 transcribes with an optional hint list (`initial_prompt`) from an external "subtitle-gen" tool
  if it exists; pass 2 corrects domain terms by exact dictionary lookup and fuzzy matching, and writes
  `uncertain_terms.json` for review (`PROJECT_OVERVIEW.md:136-137`, `src/transcriber.py:158`).
- **Model:** `WHISPER_MODEL` default `base` (`config.py:16-18`).
- **Analogy:** a stenographer who writes down the words with a time stamp on each line, then checks spellings of technical words against a glossary.

## F5. Summaries at three compression levels

- **What:** writes a structured summary. The user chooses how much to keep:
  - `high` keeps the top 20% of ranked concepts,
  - `medium` keeps 60%,
  - `low` keeps 100% (`src/summarizer.py:265-267`).
- **How it works:** the transcript is split into chunks, the LLM lists and ranks concepts per chunk, the top concepts are
  kept, and each chunk is then written up (`src/summarizer.py`, algorithm in [06-logic-and-structure.md](06-logic-and-structure.md)).
- **Outputs:** `summary.txt`, `summary_segments.json` (summary sentences linked back to timestamps), and
  `summary_metadata.json` (`app.py` writes the metadata at `app.py:561-571`).
- **Default mismatch to know about:** the home page pre-selects "high" (`templates/index.html:97`, per the review pass),
  while the summarizer function defaults to "medium" (`src/summarizer.py:242`).
- **Model:** Ollama model `llama3.2:3b` by default (`config.py:42`).
- **Analogy:** a student who first lists every idea from a lecture, marks which ones are essential, and only then writes
  the study sheet, keeping the number of ideas proportional to how short the sheet must be.

## F6. Summary timestamps

- **What:** each sentence of the summary is linked to a time in the video, so a reader can jump back to the source.
- **Implemented in:** `link_summary_to_segments()` in `src/summarizer.py` (`PROJECT_OVERVIEW.md:154` describes it).

## F7. Summary translation

- **What:** translates the summary into other languages and saves per-language copies such as `summary_<lang>.txt`.
- **Implemented in:** `src/summary_translate.py`; translation backends in `src/translators/` (see F12).
- **Downloads:** `/download/summary/<job_id>/<lang_code>` generates the file on demand (`app.py:936-952`).

## F8. Narrated audio explanation

- **What:** a spoken version of the summary, saved as `audio_explanation.mp3`.
- **Voice:** Microsoft Edge TTS (`en-US-GuyNeural` in the CLI path, `PROJECT_OVERVIEW.md:167`), with gTTS as a fallback
  (`src/tts_generator.py`).
- **Text preparation:** markdown is stripped and headings are turned into speech-friendly sentences
  (`src/text_cleaner.py`, `src/hierarchy_detector.py`, `src/natural_speech_generator.py`).
- **Download:** `/download/audio/<job_id>` (`app.py:989`). The results page offers it as "Listen" for English.

## F9. SRT subtitles

- **What:** subtitle file `summary.srt` built from the summary timestamps (`src/subtitle_generator.py`).
- **Download:** `/download/subtitles/<job_id>` (`app.py:967`). The review pass found no link to this URL in the front
  end (UNCONFIRMED whether it is used anywhere else).

## F10. Highlight videos (opt-in, up to five languages)

- **What:** a short narrated video (about 8–15 scenes) built from the source video. Each finished language is one MP4 with
  burned-in captions (`docs/AI_HANDOFF_CONTEXT.md`, section 4).
- **Languages:** English (`en`), Hindi (`hi`), Kannada (`kn`), Telugu (`te`), Tamil (`ta`) (`src/locales.py`).
- **How a user uses it:** tick "highlight video" and the languages on the home page (`templates/index.html:114-140`).
  Choosing a non-English language turns highlights on automatically (`app.py:792-793`, `static/js/app.js:91-95`).
- **Output:** `output/<job_id>/highlights/<lang>/highlight_<lang>.mp4` plus `highlights/highlight_status.json`
  (`src/highlight_jobs.py`).
- **Implemented in:** `src/highlight_pipeline.py` (orchestration), `src/highlight_chunking.py`, `src/topic_segmentation.py`,
  `src/visual_extractor.py`, `src/narration_rewrite.py`, `src/highlight_tts.py`, `src/highlight_captions.py`,
  `src/highlight_assembler.py`, `src/highlight_shared_video.py`, `src/highlight_ffmpeg.py`, `src/highlight_translate.py`.
- **Detailed stages:** [06-logic-and-structure.md](06-logic-and-structure.md), section 8.3.
- **Analogy:** a movie trailer editor. The same picture track is used for every language; only the spoken narration and the
  subtitles change per language.

## F11. Per-language status, retry, and download

- **What:** each requested language shows a row with a check mark (ready), a cross (failed, with the reason), or a neutral
  circle (not generated). Failed languages can be retried without redoing the rest.
- **How a user uses it:** results page, "Retry failed languages" (`templates/index.html:265`, `static/js/app.js:384-418`).
- **Implemented in:** `src/highlight_jobs.py` (status derivation), `app.py` (`/api/highlight/retry/<job_id>`,
  `app.py:1018`), `static/js/app.js` (`highlightRowView`, around `app.js:315-325` per the review pass).
- **Downloads and playback:** `/download/highlight/<job_id>[/<lang>]` (`app.py:1160`), and a fresh signed link for playback
  from `/api/highlight-url/<job_id>/<lang>` (`app.py:1086`).

## F12. Translation chain (text used by highlights)

- **What:** the narration text is translated through a fallback chain. Default order: Google Translate, then NLLB, then
  Ollama (`src/translators/translation_chain.py:9-27`).
- **Protection:** a list of protected terms is kept untranslated, and Indic transliteration helps keep proper nouns intact
  (`src/term_protection.py`, `src/simple_indic_translit.py`).
- **Cache:** translations are cached per job and across runs (`src/translation_cache.py`,
  `highlights/_shared/translation_cache.json`).
- **Rate-limit protection:** a circuit breaker stops Google calls for the rest of the job after the first 429 response
  (`src/translators/google_circuit.py:1-27`).
- **Environment:** `HIGHLIGHT_TRANSLATOR` and `HIGHLIGHT_TRANSLATION_CHAIN` (`src/translators/translation_chain.py:14`).

## F13. Text-to-speech per scene (highlights)

- **What:** each scene's narration is spoken with a per-language voice. Edge TTS is tried first, then the fallback voice,
  then gTTS (`src/highlight_tts.py:118-130`).
- **Voices:** see the table in [06-logic-and-structure.md](06-logic-and-structure.md), section 8.3, and `src/locales.py`.
- **Word timing:** Edge TTS word boundaries are used to time captions (`src/highlight_tts.py:42-47`).

## F14. Captions

- **What:** burned-in subtitles in the highlight videos, written as ASS files and drawn by FFmpeg/libass
  (`src/highlight_captions.py`).
- **Fonts:** a font per script, bundled in `static/fonts/` (see F19 and [07-setup-security-testing.md](07-setup-security-testing.md)).
- **Failure detection:** if FFmpeg reports a missing glyph, the language is marked failed instead of being shipped with
  empty boxes (`src/highlight_assembler.py:205-252`).

## F15. Live progress with a step checklist

- **What:** the processing screen shows an overall bar, a status line (for example "Rendering highlight video (RunPod GPU)
  · 47s"), a seven-item checklist (Download, Audio, Transcribe, Summarize, Audio generation, Subtitles, Highlight), and a log.
- **How it works:** the browser polls `/api/progress/<job_id>` every second (`static/js/app.js:3`, `:645-688`). The
  checklist follows pipeline order, not percentages, and never moves backwards (`static/js/app.js`,
  `subtaskStates`, `subtaskHighWater`).
- **Implemented in:** `app.py` (`JobProgress`, `app.py:162-251`), `src/job_progress.py` (reporting helpers),
  `static/js/app.js`.

## F16. History and results

- **What:** a history list of past jobs (newest first), and a results view with summary, languages, audio, and highlights.
- **How a user uses it:** the list is on the home page and refreshes every 5 seconds (`static/js/app.js:927-966`).
  Clicking an item opens `/job/<job_id>` (`app.py:770`).
- **Implemented in:** `app.py` (`/api/history`, `app.py:926`; `load_existing_jobs`, `app.py:120-160`),
  `static/js/app.js` (`loadJobResults`, `app.js:986-1031`).
- **Note:** history is read from disk at start-up. Jobs are never deleted by the app.

## F17. Local command-line pipeline

- **What:** runs the full pipeline from a terminal, including highlights: `python3 main.py <URL or file> --highlight-video`
  (`main.py`, `docs/AI_HANDOFF_CONTEXT.md`, section 10).
- **Default model:** `--model` defaults to `llama2` in `main.py:187`, not to `config.py`'s `llama3.2:3b`. See
  [08-history-limits-results.md](08-history-limits-results.md), section 15.

## F18. Transcript-only utility

- **What:** a standalone tool that writes only a transcript from YouTube or a local file, with no Ollama
  (`download_transcript.py`, `docs/AI_HANDOFF_CONTEXT.md`, section 14).

## F19. Local or GPU execution modes (chosen per job)

- **What:** the user can run the whole job on the laptop, or send the heavy work (transcription, summary, translation)
  and optionally the highlight rendering to RunPod (`src/pipeline_runtime.py`, `src/pipeline_executor.py`).
- **How a user uses it:** the "RunPod GPU" switch on the home page (`templates/index.html:22-28`,
  `static/js/app.js:57-69`).
- **Details:** [03-architecture.md](03-architecture.md), section 4.2.

## F20. GPU selection and deployment page

- **What:** a page at `/gpu-selection` to pick a GPU, choose which AI models run on the worker, choose the worker image
  (build it, or use a registry image), and save and deploy the RunPod serverless endpoint
  (`templates/gpu_selection.html`, `src/gpu_selection.py`, `src/compute_deploy.py`).
- **Also:** a region picker, recommendations, and a "Stop workers" and "Delete network volume" button, with a confirmation
  step for the volume (`static/js/deploy_status.js:331-358`).
- **Details:** [03-architecture.md](03-architecture.md), section 4.3 and [05-data-and-api.md](05-data-and-api.md).

## F21. Live GPU status banner

- **What:** a banner that shows whether the RunPod endpoint is running, idle, scaled to zero, or unreachable, with a
  "Last checked" time and a refresh button (`templates/partials/compute_deploy_status.html`).
- **How it updates:** the browser polls every 5 seconds while the page is visible, and the server caches results for 5
  seconds (`static/js/deploy_status.js:9`, `src/serverless_status_cache.py:15`).
- **States:** `not_deployed`, `unavailable`, `queued_max_zero`, `waiting_for_capacity`, `running`, `workers_idle_only`, `idle`
  (`src/serverless_live_status.py:19-25`). The words "queued" and "waiting" are labelled as inferences in the code.

## F22. S3 storage for every job (required)

- **What:** each job's files, including the source video, are stored in a private S3 bucket under `jobs/<job_id>/<video_uid>/`.
  History, results, playback, and downloads read from S3 only, so any laptop with the same bucket shows the same jobs.
  Jobs can be deleted from their page.
- **Required:** if S3 is not set up, new jobs are refused with a message. There is no local-only mode for storage.
- **Details:** [05-data-and-api.md](05-data-and-api.md), [06-logic-and-structure.md](06-logic-and-structure.md) §8.17, and [SETUP.md](../SETUP.md).

## F23. Capacity leases (scale-to-zero protection)

- **What:** the RunPod endpoint is kept at zero workers unless a job holds a lease. Leases are saved as JSON files and are
  released when the last job finishes (`src/endpoint_capacity.py`).
- **Why:** an idle GPU worker is billed; this keeps a finished job from leaving a worker running (`src/endpoint_capacity.py:3-4`).
- **Analogy:** a parking meter that only runs while someone is using the space, and clears itself when nobody has used it for a while.

## F24. Log redaction

- **What:** error text and log lines have secrets removed before they are shown or saved: signed-URL query strings,
  basic-auth passwords, bearer tokens, cookies, and AWS key IDs (`src/log_safety.py`).
- **Enforced by:** a static test that scans the source for unsafe logging calls (`tests/test_log_safety.py`).

## F25. Error handling on the results page

- **What:** a failed job shows its own message. Actionable errors (HTTP 422) such as "requires sign-in" are shown as they
  are; other failures return HTTP 500 (`app.py:884-896`).
