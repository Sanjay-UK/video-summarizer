# Video Summarizer — Project Documentation (index)

This is the entry point for the full project documentation. It is split into several files so each one stays readable.
Every factual claim cites a file path (and a line number where the code gives one).

**How the facts were gathered:** the code was read in full by read-only review passes (Flask layer, local text pipeline,
highlight pipeline, RunPod/compute layer, tests/CI/dependencies), and key claims were then checked by hand. Line numbers
were taken from those passes, so treat them as approximate after future edits. Where a fact could not be confirmed, the
text says `UNCONFIRMED`. Where something is missing from the repo entirely, the text says
`NOT FOUND IN CODE: <what is needed>`. All of those items are collected at the end of the last file.

**Repository state documented:** branch `main`, last commit `cf6b03a` (2026-10-26). Uncommitted changes from the session
that produced this file are listed in the section on history.

## Contents

| File | Sections | What it covers |
|---|---|---|
| [docs/01-overview.md](docs/01-overview.md) | 1–2 | What the project is, the problem, users, goals, scope, why it exists, alternatives |
| [docs/02-features.md](docs/02-features.md) | 3 | Every user-facing and internal feature, how to use it, and which module implements it |
| [docs/03-architecture.md](docs/03-architecture.md) | 4 | Components, end-to-end data flow, execution modes, design decisions, Mermaid diagrams |
| [docs/04-tech-stack.md](docs/04-tech-stack.md) | 5 | Every language, library, service and tool, with versions and reasons |
| [docs/05-data-and-api.md](docs/05-data-and-api.md) | 6–7 | Data storage (files and JSON, no database), schemas, and every HTTP endpoint |
| [docs/06-logic-and-structure.md](docs/06-logic-and-structure.md) | 8–9 | Algorithms and business rules step by step, and the annotated folder tree |
| [docs/07-setup-security-testing.md](docs/07-setup-security-testing.md) | 10–12 | Setup and deployment, security and reliability, testing and QA |
| [docs/08-history-limits-results.md](docs/08-history-limits-results.md) | 13–16 | Challenges and solutions, results and metrics, limitations and future work, timeline |
| [docs/09-glossary-and-gaps.md](docs/09-glossary-and-gaps.md) | 17 + gap list | Glossary, and the full list of `NOT FOUND` items to fill in |

## Quick orientation (one screen)

- **What it does:** takes a long educational video (a YouTube link or an uploaded file) and produces a written summary,
  optional narrated audio, optional SRT subtitles, and optional short highlight videos in up to five languages.
- **Where it runs:** a Flask web app on the user's machine (`app.py`, bound to `127.0.0.1:5000`, `app.py:1237`). Heavy
  AI work runs either locally or on RunPod serverless GPU workers (`src/pipeline_runtime.py`, `src/pipeline_executor.py`).
- **Storage:** no database. Each job is a folder under `output/<job_id>/`, and state is held in JSON files and in memory
  (`app.py:81-82`).
- **Entry points:** `app.py` (web app), `main.py` (command line), `runpod_worker/handler.py` (GPU worker),
  `download_transcript.py` (standalone transcript tool).
