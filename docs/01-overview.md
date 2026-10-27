# 1. Project Overview

[← Index](../PROJECT_DOCUMENTATION.md) · Next: [Features →](02-features.md)

## 1.1 Name and pitch

- **Name:** VideoSummarize (page title and header, `templates/index.html:6`, `templates/index.html:16`). The repository
  folder is `video-summarizer`.
- **One-line pitch:** "Turn long educational videos into readable summaries, with optional narration, subtitles and
  short multi-language highlight videos." (The README's own opening sentence says the same thing, `README.md:3`.)

**Everyday analogy:** the product is like a study assistant who watches a two-hour lecture, writes down the important
points, and then produces a one-page study sheet, a spoken recap, and short trailer clips in several languages.

## 1.2 Problem statement

Educational videos (lectures, tutorials) are long. A student who wants the key ideas has to watch the whole thing. The
project turns a video into a shorter, structured written summary. Its notes say it is built for learning: it ranks
concepts as CRITICAL, IMPORTANT or SUPPORTING rather than producing one generic paragraph (`PROJECT_OVERVIEW.md:17`).

## 1.3 Target users

- **Primary:** students and learners who watch long educational videos. The project is described as a "college /
  demo project" (`docs/AI_HANDOFF_CONTEXT.md:6`).
- **Secondary:** the developer/owner, who runs it on a personal machine and sometimes on RunPod GPUs
  (`docs/AI_HANDOFF_CONTEXT.md:6`).
- **Language audience:** English, Hindi, Kannada, Telugu and Tamil for highlight videos
  (`src/locales.py`, see [02-features.md](02-features.md)).

NOT FOUND IN CODE: formal user research, personas, or a list of intended end users beyond the notes above.

## 1.4 Goals

Taken from the design principles in the handoff document (`docs/AI_HANDOFF_CONTEXT.md:14-18`) and the code:

1. **Local-first privacy:** transcription and summarization run on the user's machine by default (Whisper and Ollama,
   `PROJECT_OVERVIEW.md:3`).
2. **Concept-driven compression:** the user chooses how much to keep (`high`, `medium`, `low`), and the number of
   concepts kept is a fraction, not a fixed word count (`src/summarizer.py:265-267`).
3. **Optional GPU speed-up:** when the local machine is too slow, the heavy work can run on RunPod
   (`src/pipeline_runtime.py`).
4. **Repeatable outputs:** the LLM calls use temperature 0 and a fixed seed (`src/highlight_llm.py`,
   `config.py:72-73`).

## 1.5 Scope (what the project does)

- Accepts a YouTube URL or an uploaded video file (`app.py:778`, `src/downloader.py`).
- Extracts audio, transcribes it with timestamps, and writes a summary (`src/audio_extractor.py`,
  `src/transcriber.py`, `src/summarizer.py`).
- Optionally translates the summary (`src/summary_translate.py`).
- Optionally produces narrated audio (`src/tts_generator.py`), SRT subtitles (`src/subtitle_generator.py`), and
  per-language highlight videos (`src/highlight_*.py`).
- Offers a history list and a results view in the browser (`templates/index.html`, `static/js/app.js`).
- Offers a GPU selection page to deploy, inspect and stop RunPod resources (`templates/gpu_selection.html`,
  `src/compute_deploy.py`).

## 1.6 Out of scope (what the code does not do)

Checked against the code; each item is absent or explicitly limited:

| Not supported | Evidence |
|---|---|
| User accounts, login, or access control | No auth code anywhere in `app.py`, `src/`, or `runpod_worker/` (checked by search). Binding to `127.0.0.1` is the only protection (`app.py:1237`). |
| A database | No `sqlite3`, SQLAlchemy, Postgres or Mongo code (checked by search). State is files and memory. |
| Cancelling a running job | No cancel route (review pass, `app.py` route table). "Back" only hides the progress screen (`static/js/app.js:861-883`). |
| Deleting jobs or outputs from the UI or API | No `rmtree`, `unlink` or `os.remove` in `app.py` (review pass). |
| Multi-user or multi-machine job coordination | Job state is in process memory (`app.py:81`). Capacity leases are local JSON files (`src/endpoint_capacity.py:3-4`, `:13-17`). |
| Audio-track switching in one video | Each language is a separate MP4 (`docs/AI_HANDOFF_CONTEXT.md`, section 4.2, "One MP4 per language"). |
| Live video editing or uploading to YouTube | Not present. |
| Automatic backups | NOT FOUND IN CODE (no backup logic exists). |

## 1.7 Why it exists (background)

The project started as a Week-1 foundation (download, extract audio, transcribe) on a laptop with limited memory
(`README.md`, "Quick Start (Week 1: Foundation)" and the 8 GB RAM troubleshooting section). It grew into a full web app as later phases were added. The git history
shows the stages (see [08-history-limits-results.md](08-history-limits-results.md), section 16).

The handoff document lists the phase names used in conversation: Phase 1 = English highlight video, Phase 2 = five-language
highlights with a translation chain, and Phase 3 = production GPU reliability (`docs/AI_HANDOFF_CONTEXT.md:417`).

## 1.8 Alternatives and the gap it fills

NOT FOUND IN CODE: a written competitor review. The repository does not compare the project with other tools.

What the code does show about the choices:

| Alternative | Why this project does something different (from code or notes) |
|---|---|
| Cloud summarization APIs | The project is local-first for privacy (`PROJECT_OVERVIEW.md:3`). Ollama runs the LLM locally (`src/summarizer.py`). |
| Whisper hosted in the cloud | Whisper runs locally with faster-whisper; RunPod is optional (`src/transcriber.py`, `src/runpod_serverless.py`). |
| Generic one-paragraph summaries | Ranked concepts with compression levels (`src/summarizer.py:242-267`). |
| Machine-translated subtitles only | Highlight videos use a translation chain with a protected-terms list and a cache (`src/translators/translation_chain.py`, `src/term_protection.py`). |

The gap the project fills, in the words of its own notes: a learner-focused tool that keeps the user's own machine in the
loop, adds narrated and multi-language short videos, and can use a rented GPU only when needed.

## 1.9 Status at the time of documentation

- Branch `main`, last commit `cf6b03a` on 2026-10-26 (`git log`).
- The text path (download → transcribe → summarize → optional audio/SRT) and the highlight path are both present in code.
- The RunPod serverless path is present and was used in real runs (the job folder `output/b3848d67/` contains
  `runpod_job_metrics.json` with a real `executionTime`; the folder is generated data and is not committed).
- Known open problems are listed in [08-history-limits-results.md](08-history-limits-results.md), section 15.
