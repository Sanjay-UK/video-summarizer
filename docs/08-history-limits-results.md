# 8. Challenges, Results, Limitations and History

[← Setup, Security, Testing](07-setup-security-testing.md) · [Index](../PROJECT_DOCUMENTATION.md) · Next: [Glossary and Gaps →](09-glossary-and-gaps.md)

Sections 13 (challenges and solutions), 14 (results and metrics), 15 (limitations and future work) and 16 (timeline).

---

# 13. Challenges and Solutions

Each challenge is taken from the git history (commit messages, dated), from code comments, or from the handoff notes. The
commit hash is given where one exists.

**Everyday analogy for this section:** a logbook of the breakdowns and repairs on a car. Each entry says what broke, why,
and what was changed.

## 13.1 YouTube blocks some download clients

- **Problem:** YouTube sometimes refuses one client profile with a sign-in or bot check.
- **Solution:** try four client profiles in turn (`src/downloader.py:25-30`), and classify each failure into a code with a
  clear user message (`src/youtube_errors.py`). Sign-in and private-video errors are not retried, because retrying cannot help.
- **Commit:** `cf6b03a` "Fix video processing errors, subtitles, and progress reporting" (2026-10-26).

## 13.2 Caption boxes instead of letters for Indic scripts

- **Problem:** the base GPU image has no Indic fonts, so FFmpeg/libass drew empty boxes for Hindi, Kannada, Tamil and Telugu.
- **Solution:** ship the Noto fonts in `static/fonts/`, copy them into the worker image and register them with fontconfig
  (`runpod_worker/Dockerfile:28-33`). Detect missing glyphs in FFmpeg's output and fail the language instead of shipping
  boxes (`src/highlight_assembler.py:205-252`).
- **Also found:** the DejaVu Sans file in the repo was actually an Arial font under the DejaVu name. It was replaced with the
  official DejaVu Sans 2.37 release, checked by checksum and font name, and the licence was added
  (`static/licenses/DejaVu-LICENSE.txt`). This was done in the session that produced this document and is **not yet committed**.
- **Verification:** the worker image was built locally and the rendering tests passed inside it, including the negative
  control (without fonts, the glyph failure is detected).
- **Lesson recorded in the code:** a style that names a family the bundled file does not declare falls back silently; a test
  now checks each caption style against its file's family name (`tests/test_subtitle_fonts.py`).

## 13.3 Torch replaced by CPU build during installation

- **Problem:** installing `transformers` through normal pip replaced the CUDA build of PyTorch in the base image with a CPU
  build, so NLLB failed with "PyTorch not found" (`docs/AI_HANDOFF_CONTEXT.md`, section 6.2).
- **First attempt:** install `transformers` with `--no-deps`. That failed because `regex` was missing (handoff, section 6.3).
- **Solution:** install the hashed lock file offline, then assert the torch and transformers versions at build time
  (`runpod_worker/Dockerfile:17-24`). Transformers is pinned to 4.49.0 for the torch.load security issue (CVE-2025-32434,
  `runpod_worker/Dockerfile:14`).
- **Commit:** `e9e1d22` "Add RunPod Serverless GPU deploy path with automated worker image publish" (2026-10-24).

## 13.4 RunPod pods: SSH unreliable from home networks

- **Problem:** the pod's SSH proxy hung (banner timeouts), and direct TCP was unreliable from the owner's network
  (`docs/AI_HANDOFF_CONTEXT.md`, section 6.1).
- **Solution:** a Jupyter-proxy transport for pods, chosen automatically when SSH is the wrong host or the probe fails
  (`src/pipeline_executor.py:332-390`, `src/runpod_jupyter.py`).
- **Commit:** `4f8d74f` "Add hybrid RunPod GPU pipeline with Jupyter file transfer fallback" (2026-10-24).
- **Later:** the serverless path (`e9e1d22`) avoids SSH entirely.

## 13.5 Requests larger than RunPod's limit

- **Problem:** RunPod's `/run` body is limited to about 10 MB; audio and video were larger.
- **Solution:** re-encode the audio to smaller MP3 bit rates, and the video to smaller sizes, until under 9 MB
  (`src/runpod_serverless_payload.py:78-127`, `:343-383`). For highlights, upload the source to S3 and send a signed link
  instead (`src/runpod_serverless_payload.py:397-408`, `src/s3_video_store.py`).
- **Commit:** S3 source storage in `96c6f78` (2026-10-26).

## 13.6 Idle GPUs billed when nothing was running

- **Problem:** a GPU endpoint left with workers running costs money with no work to do.
- **Solution:** capacity leases: the endpoint stays at zero workers unless a job holds a lease; a background sweep corrects it
  every five minutes (`src/endpoint_capacity.py`, `app.py:1231-1232`).
- **Commit:** `96c6f78` "Add endpoint capacity lifecycle, S3 source storage, and cross-platform CI" (2026-10-26).
- **Remaining gap:** Stop and Delete-volume bypass the lease (section 15).

## 13.7 Progress bar going backwards, or showing 100% too early

- **Problem:** several progress sources (local stages, RunPod, late poll responses) disagreed; the bar could go backwards,
  and the checklist fell behind the status line.
- **Solution:** progress only moves forward and is capped at 99% until the job completes (`app.py:193`, `:222`, `:201`);
  stale responses are discarded by a sequence and generation check (`static/js/app.js`, `shouldApplyPoll`); the checklist
  follows pipeline order (`static/js/app.js`, `subtaskStates`) with a high-water mark.
- **Commits:** `cf6b03a` (2026-10-26); the checklist change is **not yet committed** (session of 2026-10-26).

## 13.8 Highlight row said "Failed" for a language nobody asked for

- **Problem:** the default language "en" was always listed as requested, so an unrequested language showed as failed.
- **Solution:** store `requested_languages` explicitly, and show only requested languages (`app.py`, `src/highlight_jobs.py`).
- **Commit:** part of the highlight work in `cf6b03a` (2026-10-26).

## 13.9 Secrets could appear in error text and logs

- **Problem:** exception messages can contain signed URLs, tokens and cookies; several log sites printed them.
- **Solution:** a redaction module (`src/log_safety.py`), applied at every log and error site, with a static test that finds
  unsafe calls, including calls split over several lines (`tests/test_log_safety.py`).
- **Commit:** part of `cf6b03a` (2026-10-26). The static guard was added after a multi-line bypass was found in review.

## 13.10 Windows CI failures

- **Problem 1:** the upload folder was hard-coded to `/tmp`, which does not exist on Windows.
  **Fix:** use the platform temporary folder (`app.py:83-84`), commit `4febcbd` (2026-10-26).
- **Problem 2:** Node test output was decoded with the wrong text encoding on Windows.
  **Fix:** decode as UTF-8, commit `38716a3` (2026-10-26).

## 13.11 Multi-language rendering: wrong voices and timing

- **Problem:** languages needed their own voices, fonts and caption timing; longer translations overflowed scenes.
- **Solution:** a single locale registry (`src/locales.py`) with voice chains and fonts; per-language narration; one shared
  picture track; translations cut at the English scene length (`src/highlight_assembler.py:57-59`).
- **Commits:** `49da4af` "Add Phase 2 multilanguage highlights with offline translation chain" (2026-10-20);
  `2068dfb` "Improve pipeline speed and five-locale translation quality" (2026-10-23).

## 13.12 Google Translate rate limits

- **Problem:** Google returns "too many requests" when called in bursts. A comparison run recorded this failure
  (`translations_compare.md`, generated 2026-10-18).
- **Solution:** a process-wide minimum gap between calls (`src/translators/google_backend.py:55-68`); growing waits on
  rate-limit errors; a per-job circuit breaker that stops Google for the rest of the job (`src/translators/google_circuit.py`);
  fallback to NLLB and then Ollama (`src/translators/translation_chain.py`).

## 13.13 Summaries that lost topics

- **Problem:** an early design re-summarised the summary, which dropped topics.
- **Solution:** write each chunk from its own selected concepts and do not re-summarise (`src/summarizer.py:598-633`;
  `PROJECT_OVERVIEW.md`, Step 4, "no second re-compression pass").

## 13.14 Ambiguous file-system state: parallel languages

- **Problem found in this review:** the caption (ASS) file is copied to a temporary path named from the scene folder name
  (`src/highlight_assembler.py:194-198`). Every language uses a folder called `scene_NNN`, so parallel languages can write the
  same temporary file. **Not reproduced**, but the code allows it. This is a real risk and should be fixed by using a unique
  path per language.

## 13.15 Serverless job submission and duplicate jobs

- **Problem:** a timeout or gateway error after RunPod accepted a job could cause a second, paid job if retried.
- **Solution:** retry only an explicit "paused" response (409); never retry 502–504 (`src/runpod_serverless.py`, `submit_job`).
- **Commit:** this behaviour was changed in the session of 2026-10-26; the error message change is not yet committed.

---

# 14. Results and Metrics

## 14.1 Runtime measurements that exist

| Measurement | Value | Source |
|---|---|---|
| RunPod queue delay for one heavy job | `delayTime` 14,109 ms | `output/b3848d67/runpod_job_metrics.json` (generated, not committed) |
| RunPod execution time for that job | `executionTime` 140,611 ms (about 2 min 20 s) | same file |
| Heavy job model | Whisper `base` on CUDA (`output/b3848d67/heavy_done.json` shows `whisper_model: "base"`) | same folder |
| Source video size in that job | 6,865,192 bytes (about 6.9 MB) | `output/b3848d67/source.mp4` |
| Highlight job timings (example fields) | `tts_seconds`, `render_seconds`, `total_seconds` per language | `src/highlight_pipeline.py:288-297`; example in [05](05-data-and-api.md), section 6.4 |

These are single runs. NOT FOUND IN CODE: a set of repeated measurements, so no averages or variance can be given.

## 14.2 Benchmarks

- A benchmark script exists: `scripts/run_benchmark_b.py` (Google live, optional). Its output is **not** committed.
  NOT FOUND IN CODE: benchmark results for the summary, transcription or highlight stages.
- The handoff notes say Whisper on a CPU laptop with the `base` model runs at about 2–3 times real time
  (`README.md`, "Memory issues on 8GB RAM laptop", and the troubleshooting notes). These are the owner's notes, not measured in code.

## 14.3 Translation comparison (a recorded run)

`translations_compare.md` (generated 2026-10-18, 92 lines) compares the three backends on three scenes from a
sushi lecture, with English narration word-capped at `high` compression.

What the recorded run shows:
- **Google for Hindi:** failed with a "too many requests" error for the scenes shown (the run was not repeated after the
  throttle was added).
- **NLLB:** "skipped — NLLB not installed" on that machine.
- **Protected terms per scene:** for example "sushi", "maki", "nigiri", "kombu", "umami" are kept as protected terms.

The file's own note says to re-run `scripts/compare_translators.py` after installing `deep-translator` and
`indic-transliteration` for complete Google rows.

## 14.4 Sample inputs and outputs

| Sample | Where | Notes |
|---|---|---|
| Highlight translation fixture | `scripts/fixtures/highlight_smoke_translations.json` | Test-only translations used by the fixture backend (`src/translators/fixture_backend.py`) |
| Lecture fixture generator | `scripts/create_lecture_fixture.py` | Creates a test lecture video with FFmpeg |
| Sample status document | [05-data-and-api.md](05-data-and-api.md), section 6.4 | Illustrative |
| Sample progress response | [05-data-and-api.md](05-data-and-api.md), section 7.2 | Illustrative |
| Real job folder | `output/<job_id>/` on the owner's machine | Generated data; not committed |

NOT FOUND IN CODE: a committed sample video, sample summary, or sample highlight MP4 for public use.

## 14.5 Verification record (this documentation pass)

- Python suite: 420 tests, OK, 3 skipped (macOS, with the libass FFmpeg).
- Worker image built locally (`video-summarizer-worker:fonts-verify`); the subtitle tests passed inside it on Linux.
- Browser check of the checklist (real Chrome, local app, scripted progress responses): passed with no page errors.
- Real RunPod run: one heavy job completed (the metrics above). No paid job was started during this documentation pass.

## 14.6 Screenshots worth capturing

These are not in the repo. Capture them from a running app for the report or presentation:

1. **Home page** — the input tabs, the compression choices, and the highlight languages.
2. **Processing screen** — the overall bar, the status line "Rendering highlight video (RunPod GPU)", and the seven-item checklist with Highlight active.
3. **Results page** — the summary card, the language selector, and the highlight card with a player.
4. **Highlight row list** — one success tick, one failed cross with its reason, one "Not generated" circle.
5. **A highlight frame with Kannada and Hindi captions** — from `frame_kn.png` and `frame_mixed.png` (rendered during the session;
   stored outside the repo in `/tmp`).
6. **GPU selection page** — the deploy status banner in the "Scaled to zero" and "Running" states.
7. **Terminal output** — `python3 main.py ... --highlight-video` showing the steps.
8. **History list** — several past jobs with their status.

---

# 15. Limitations and Future Work

## 15.1 TODO, FIXME and HACK markers

NOT FOUND IN CODE: any `TODO`, `FIXME`, `HACK` or `XXX` markers in the source (checked in `src/`, `app.py`, `main.py`,
`config.py`, `download_transcript.py`, `templates/`, `static/js/`, `runpod_worker/`). The only related text is a usage
placeholder in `download_transcript.py:14`.

## 15.2 Known defects found in the code

Grouped by area. Each was read from the code; the ones marked "reproduced" were also run.

**Main command line and local pipeline**

| # | Defect | Where | Effect |
|---|---|---|---|
| D1 | A failed Week-1 step can still return partial outputs, and `main.py` then prints success and exits 0 | `main.py:69-71`, `:226-230` | A failed run can look successful |
| D2 | If both TTS engines fail, an empty `audio_explanation.mp3` is written and success is reported | `src/tts_generator.py:239-243` | Empty audio file presented as valid |
| D3 | If `edge_tts` is missing, the code runs `pip install` and fails; gTTS is never tried | `src/tts_generator.py:203-210` | Narration fails even though a fallback exists |
| D4 | Rate markers are stripped before Edge TTS, so pacing settings have no effect | `src/tts_generator.py:60-69` | Speech pacing not as designed |
| D5 | Every sentence without a transcript match is linked to the start of the video | `src/summarizer.py:484-534` | Wrong timestamps in subtitles and results |
| D6 | Substring replacement in term correction can change part of a longer word | `src/transcriber.py:44-48` | Silent word damage |
| D7 | A subtitle millisecond value can round to 1000, producing a malformed time | `src/subtitle_generator.py:18` | Invalid SRT timestamp |
| D8 | A leftover `.mkv` or `.webm` file is renamed to `.mp4` without remuxing | `src/downloader.py:160-165` | Player errors on the renamed file |
| D9 | If FFmpeg is missing, the audio step calls `sys.exit(1)` | `src/audio_extractor.py:48-50` | Stops the process rather than reporting an error to the caller |

**Web app and highlights**

| # | Defect | Where | Effect |
|---|---|---|---|
| D10 | Compression and job ID not validated on several routes | `app.py:784`, `:936-1008` | See section 11.6 |
| D11 | "Download Audio" downloads a highlight video for non-English languages | `static/js/app.js:896-905` | Wrong file under the audio label (confirmed by reading the code) |
| D12 | The UI sends a `model` field that the server ignores | `app.py:783`, `static/js/app.js:553` | Harmless but misleading |
| D13 | Highlight scene count is decided by the LLM; compression does not change it | `src/topic_segmentation.py` | Compression does not reduce scenes as a user might expect |
| D14 | The fallback summary text ("Summary unavailable…") can be spoken aloud | `src/highlight_pipeline.py:137-144` (UNCONFIRMED at run time) | Odd narration in a failure case |
| D15 | Shared temporary caption file collides between parallel languages | `src/highlight_assembler.py:194-198` | Possible wrong captions (not reproduced) |
| D16 | `FFMPEG_PATH` help text asks for a folder, but the code requires a file | `src/highlight_ffmpeg.py:17`, `:60` | A folder value is ignored silently |

**RunPod and deployment**

| # | Defect | Where | Effect |
|---|---|---|---|
| D17 | Stop and Delete-volume bypass the capacity lease | `src/compute_deploy.py:479-504`, `:530-534` | Workers can be stopped during a job (UNCONFIRMED) |
| D18 | Pod-mode log says the summary runs on the GPU; on the Jupyter path it runs locally | `src/pipeline_executor.py:330` | Misleading log |
| D19 | `.cache/worker-wheels` is needed by the Dockerfile but gitignored and untracked | `runpod_worker/Dockerfile:20`, `.gitignore:22` | A fresh clone cannot build the worker image |
| D20 | The docs say oversized videos fall back to local FFmpeg; the code records a failure | `docs/RUNPOD_SERVERLESS.md:66` vs `src/runpod_serverless_payload.py:378-383` | Documentation is wrong |
| D21 | Docs use tag `v1.0.0`; the code defaults to `v1.0.11` | `docs/RUNPOD_SERVERLESS.md:27` vs `src/serverless_worker_image_publish.py:18` | Documentation is out of date |
| D22 | `requirements-worker.txt` is stale and not used by the Dockerfile | `runpod_worker/requirements-worker.txt:7-8` | Confusing for readers |
| D23 | `RUNPOD_NETWORK_VOLUME_ID` is listed twice in `.env.example` | `.env.example:7`, `:13` | Minor |

## 15.3 Documentation that disagrees with the code

| Document | Says | Code does | Where |
|---|---|---|---|
| `SETUP.md:10` | Python 3.9+ | 3.10+ required | `config.py:22`, `app.py:290` |
| `SETUP.md:66-67` | Install with `requirements.txt` | CI and launcher use the hashed locks | `.github/workflows/tests.yml:32,36` |
| `SETUP.md:70-77` | Seven packages | Eleven in `requirements.txt` | `requirements.txt` |
| `README.md:64-74` | Folder tree with `src/main.py`, `youtube_downloader.py`, `segment_linker.py`, `outputs/` | Root `main.py`, `src/downloader.py`, no `segment_linker`, `output/` | repo layout |
| `README.md:68`, `:81` | Piper TTS | Edge TTS and gTTS | `src/tts_generator.py` |
| `README.md:136` | `terms.json` | `uncertain_terms.json` written | `src/transcriber.py:158` |
| `README.md:107`, `:112` | Hard-coded path to an outside subtitle-gen tool | Outside this repo | NOT FOUND IN REPO |
| `README.md:96-98` | 720p recommended | 360p default | `config.py:29`, `main.py:186` |
| `README.md:139-141` | Weeks 2 and 3 in progress | Both present in code | repo |
| `PROJECT_OVERVIEW.md:99-102` | Web app stops after summary; TTS and SRT not wired in | Routes for subtitles, audio and highlights exist | `app.py:966-989`, `:1017-1159` |
| `PROJECT_OVERVIEW.md:176-186` | Partial API and progress lists | Incomplete | `app.py` |
| `docs/AI_HANDOFF_CONTEXT.md` | Open issues: v1.0.3 build fails, warmup fails | Superseded by later commits | `runpod_worker/Dockerfile` |
| `docs/AI_HANDOFF_CONTEXT.md:258-260` | SETUP and PROJECT_OVERVIEW under `docs/` | At the repo root | repo |
| `docs/S3_SOURCE_RETENTION_PLAN.md` | Proposal | Not implemented | `src/s3_video_store.py` |

## 15.4 Known limits by design

- One machine only; job state in memory; no cancel, no delete, no multi-user access (section 1.6).
- No retries on gateway errors for RunPod (by design, to avoid duplicate paid jobs).
- No automatic cleanup of uploads, job folders, or the temporary caption files.
- Highlight length is decided by the LLM and the English narration length, not the target duration.
- Translation quality depends on the back end; Google is unofficial and rate-limited.
- Captions are burned in with a single style (`src/highlight_captions.py:146`).

## 15.5 Planned or suggested future work (from the project's own notes)

| Item | Status | Source |
|---|---|---|
| Worker dependency fix | Done in code; image built locally as `video-summarizer-worker:fonts-verify`; not pushed or deployed | `runpod_worker/Dockerfile`, section 13.3 |
| End-to-end GPU job with multi-language highlights, watching payload size | Suggested | `docs/AI_HANDOFF_CONTEXT.md`, section 12 |
| Update `PROJECT_OVERVIEW.md` to match `main` | Suggested; not done | `docs/AI_HANDOFF_CONTEXT.md`, section 12 |
| Decide whether `data/compute_selection.json` should have a committed template | Suggested | `docs/AI_HANDOFF_CONTEXT.md`, section 12 |
| S3 lifecycle rule and retention policy | Proposal only | `docs/S3_SOURCE_RETENTION_PLAN.md` |
| Fix the shared caption temp file, the listen-button label, the upload filename, and the lease bypass | Found in this review; not yet fixed | sections 11.6, 13.14, 15.2 |
| Consistent compression behaviour for scene count | Not planned in any file | NOT FOUND IN CODE |

---

# 16. Project Timeline

## 16.1 Commits on `main` (from `git log`, dated)

| Date | Commit | Message | Milestone |
|---|---|---|---|
| 2026-08-25 | `91c180d` | Initial commit: automatic video summarization pipeline | Core text pipeline |
| 2026-08-25 | `fe12035` | Add detailed setup guide for fresh machine installs | Setup documentation |
| 2026-10-18 | `c1711fa` | Fix main.py indentation so the CLI pipeline runs again | CLI repaired |
| 2026-10-18 | `a4ba3c4` | Add Phase 1 English highlight pipeline (topics, visuals, TTS, assembly) | Highlight videos, English |
| 2026-10-18 | `5c64af2` | Wire highlight video opt-in through the web UI and download route | Web UI for highlights |
| 2026-10-18 | `ebb3959` | Document highlight video setup for macOS and Windows FFmpeg requirements | FFmpeg documentation |
| 2026-10-20 | `49da4af` | Add Phase 2 multilanguage highlights with offline translation chain | Five languages |
| 2026-10-23 | `2068dfb` | Improve pipeline speed and five-locale translation quality | Speed and quality |
| 2026-10-24 | `4f8d74f` | Add hybrid RunPod GPU pipeline with Jupyter file transfer fallback | Pod GPU path |
| 2026-10-24 | `e9e1d22` | Add RunPod Serverless GPU deploy path with automated worker image publish | Serverless GPU path |
| 2026-10-26 | `96c6f78` | Add endpoint capacity lifecycle, S3 source storage, and cross-platform CI | Capacity leases, S3, CI on macOS and Windows |
| 2026-10-26 | `bd39a8f` | Fix heavy-job metrics NameError after submission refactor | Bug fix |
| 2026-10-26 | `7beae31` | Point worker image default to v1.0.11 | Image version |
| 2026-10-26 | `174cb07` | Store S3 highlights with fresh one-hour links; bind app to localhost | Signed links, localhost binding |
| 2026-10-26 | `4febcbd` | Use platform-specific temporary directory for uploads | Windows fix |
| 2026-10-26 | `36744de` | Show live worker status on GPU page with cached, read-only polling | Live status |
| 2026-10-26 | `38716a3` | Decode Node test output as UTF-8 for Windows CI | Windows fix |
| 2026-10-26 | `cf6b03a` | Fix video processing errors, subtitles, and progress reporting | Latest commit on `main` |

Authors: one author, `uks`, with 19 commits across all branches (`git shortlog -sn --all`).

## 16.2 Branches

| Branch | Last commit date | Role (from the handoff notes) |
|---|---|---|
| `main` | 2026-10-26 | Full product |
| `initial` | 2026-09-16 | Reduced version for a teacher demo, upload-only |
| `feature/key-scene-output-multilang-summary` | 2026-10-18 | Early highlight work |
| `phase2-multilang-highlights` | 2026-10-20 | Phase 2 work |
| `feature/speed-and-translation-quality` | 2026-10-23 | Speed and translation quality |
| `serverless-migration` | 2026-10-24 | Serverless endpoint and worker image |
| `cursor/runpod-jupyter-file-transfer` | 2026-10-24 | Pod Jupyter fallback |

Source: `git branch -a` and `docs/AI_HANDOFF_CONTEXT.md`, section 2.

## 16.3 Phases (from the handoff notes)

| Phase | Meaning | Where |
|---|---|---|
| Week 1 | Download, audio, transcription | `README.md` |
| Week 2 | Summarisation | `PROJECT_OVERVIEW.md` |
| Week 3 | Subtitles, narration, CLI integration | `README.md` |
| Phase 1 | English highlight video | `docs/AI_HANDOFF_CONTEXT.md`, section 4.1 |
| Phase 2 | Five-language highlights with translation chain | `docs/AI_HANDOFF_CONTEXT.md`, section 4.2 |
| Phase 3 | Production GPU reliability (informal) | `docs/AI_HANDOFF_CONTEXT.md`, section 13 |

## 16.4 Uncommitted work at the time of this document

Changes made in the session of 2026-10-26 and not committed (from `git status`):
- `static/fonts/DejaVuSans.ttf` (replaced with the official release);
- `static/licenses/DejaVu-LICENSE.txt` (new);
- `static/js/app.js` (order-based checklist);
- `src/locales.py` (back to the original name, so no net change now);
- `tests/test_poll_and_results_js.py`, `tests/test_subtitle_fonts.py` (new tests);
- `src/runpod_serverless.py`, `tests/test_job_readiness.py` (gateway error message and tests);
- the documentation files created for this task.

Untracked files that are not part of the project and must stay out of Git: the presentation and PDF files at the repo root,
`transcripts/`, and the four deploy scripts listed in the session notes.
