# 9. Glossary and Gaps

[← History, Limits, Results](08-history-limits-results.md) · [Index](../PROJECT_DOCUMENTATION.md)

Section 17 (glossary), followed by the complete list of `NOT FOUND` items to fill in.

---

# 17. Glossary

Plain-language meanings. Each entry says where the term appears in this project.

## 17.1 Product and job terms

| Term | Plain meaning | Where it appears |
|---|---|---|
| **Job** | One request to process one video, with its own folder and ID | `app.py:817`, `output/<job_id>/` |
| **Job ID** | An 8-character code for a job, for example `a1b2c3d4` | `app.py:817` |
| **Summary** | A structured written version of the lecture | `src/summarizer.py` |
| **Compression** | How much of the lecture's ideas to keep: `high` (20%), `medium` (60%), `low` (100%) | `src/summarizer.py:265-267` |
| **Concept** | One idea pulled out of a transcript chunk, tagged with its importance | `src/summarizer.py:147` |
| **Narration** | Spoken audio of the summary | `src/tts_generator.py` |
| **Highlight video** | A short narrated video made of about 8–15 scenes, one per language | `src/highlight_pipeline.py` |
| **Scene** | One piece of a highlight video: a picture, a narration, and captions | `src/highlight_pipeline.py` |
| **Topic** | A section of the lecture, found by the LLM, which becomes a scene | `src/topic_segmentation.py` |
| **Still / clip / placeholder** | The three kinds of picture a scene can use | `src/visual_extractor.py:229-303` |
| **Caption** | On-screen subtitle text burned into a video | `src/highlight_captions.py` |
| **Locale** | A language's settings: voices, font, and speech-service language | `src/locales.py` |
| **Retry (highlight)** | Rebuilding only the languages that failed | `app.py:1018`, `src/highlight_pipeline.py:469-509` |
| **History** | The list of past jobs shown on the home page | `app.py:926` |

## 17.2 Speech and text terms

| Term | Plain meaning | Where it appears |
|---|---|---|
| **Transcript** | The written text of the spoken words | `transcript.txt` |
| **Segment** | A short piece of speech with a start and end time | `segments.json`, `src/transcriber.py` |
| **Timestamp** | The time in the video, for example `00:03:12` | SRT files, `summary_segments.json` |
| **SRT** | A standard subtitle file: numbered times and text | `src/subtitle_generator.py` |
| **ASS** | A richer subtitle file used for burned-in captions, with styling | `src/highlight_captions.py` |
| **Term correction** | Fixing words that speech-to-text gets wrong, using a dictionary | `src/transcriber.py:50-85` |
| **Protected term** | A name or loanword kept unchanged during translation | `src/term_protection.py` |
| **Transliteration** | Writing a word in another script by sound, for example in Kannada letters | `src/simple_indic_translit.py` |
| **Translation chain** | A list of translators tried in order when one fails | `src/translators/translation_chain.py` |
| **Circuit breaker** | A switch that stops calls to a failing service for the rest of a job | `src/translators/google_circuit.py` |
| **Quality gate** | A check that a translation really is in the target script and not junk | `src/translation_quality.py` |
| **Word boundary** | The start and end time of each spoken word, used to time captions | `src/highlight_tts.py:42-47` |
| **gTTS / Edge TTS** | Two text-to-speech services: Google's (`gTTS`) and Microsoft's Edge voices | `src/tts_generator.py`, `src/highlight_tts.py` |
| **Voice** | A named synthetic speaker, for example `hi-IN-SwaraNeural` | `src/locales.py` |
| **Markdown** | Light text formatting with `#` headings and `-` bullets; stripped before speech | `src/text_cleaner.py` |

## 17.3 AI and model terms

| Term | Plain meaning | Where it appears |
|---|---|---|
| **LLM** | Large language model: a text-writing AI | `src/summarizer.py`, `src/highlight_llm.py` |
| **Ollama** | A program that runs language models on your own machine, with an HTTP API | `config.py:41` |
| **Whisper / faster-whisper** | A speech-to-text model, and a fast version of it | `src/transcriber.py` |
| **NLLB-200** | A Meta translation model covering many languages, run offline | `src/translators/nllb_backend.py` |
| **Temperature** | How random a model's answer is; 0 means the most predictable answer | `src/highlight_llm.py:1` |
| **Seed** | A fixed starting number that makes a random process repeat exactly | `config.py:73` |
| **Context window (`num_ctx`)** | How much text the model can read at once | `src/topic_segmentation.py:89` |
| **Inference** | Running a trained model to get an answer | Throughout |
| **CUDA** | NVIDIA's GPU programming platform | `runpod_worker/Dockerfile:1` |
| **Torch (PyTorch)** | The machine-learning library the NLLB model uses | `requirements/mac-cpu.lock` |

## 17.4 Video and file terms

| Term | Plain meaning | Where it appears |
|---|---|---|
| **FFmpeg** | A command-line program that cuts, converts and joins video and audio | `src/highlight_ffmpeg.py` |
| **libass** | A library FFmpeg uses to draw subtitles; needed for burned-in captions | `src/highlight_ffmpeg.py:107-120` |
| **ffprobe** | A companion to FFmpeg that reads a file's length and details | `src/highlight_ffmpeg.py:65-72` |
| **MP4 / MP3 / JPG / PNG** | Common video, audio and picture file formats | Job folders |
| **Codec / CRF** | How video is compressed; CRF sets the quality (lower is better) | `src/runpod_serverless_payload.py` |
| **Bit rate (kbps)** | How much data per second an audio file uses | `src/runpod_serverless_payload.py:78-127` |
| **Sharpness score** | How crisp a picture is, measured by the variance of an edge filter | `src/visual_extractor.py:89-100` |
| **Motion score** | How much a scene changes between frames | `src/visual_extractor.py:103-131` |
| **Muxing** | Combining a picture track and a sound track into one file | `src/highlight_assembler.py:213-253` |
| **Concatenation** | Joining several clips end to end | `src/highlight_assembler.py:303-333` |
| **Filter graph** | FFmpeg's chain of effects, written as one text string | `src/highlight_assembler.py` |

## 17.5 Cloud and deployment terms

| Term | Plain meaning | Where it appears |
|---|---|---|
| **RunPod** | A cloud service that rents GPUs by the second | `src/runpod_serverless.py` |
| **Serverless endpoint** | A RunPod address that starts a GPU worker when a job arrives | `src/runpod_serverless.py` |
| **Pod** | A rented GPU computer you log into (older path in this project) | `src/compute_deploy.py`, `src/runpod_ssh.py` |
| **Worker** | The program running on a GPU that does the heavy jobs | `runpod_worker/handler.py` |
| **Cold start** | The delay while a worker starts up and loads models | `docs/RUNPOD_SERVERLESS.md` |
| **Scale to zero** | Keeping no workers running when there is no work | `src/endpoint_capacity.py` |
| **Lease** | A time-limited note that a job needs the endpoint; prevents scaling to zero during the job | `src/endpoint_capacity.py` |
| **Network volume** | A disk that survives worker restarts, used to keep model files | `src/runpod_serverless.py:123-136` |
| **Docker image** | A packaged copy of the worker's software and libraries | `runpod_worker/Dockerfile` |
| **Docker Hub** | A public place to store Docker images | `src/serverless_worker_image_publish.py` |
| **Dockerfile** | A recipe for building a Docker image | `runpod_worker/Dockerfile` |
| **Payload** | The data sent in one request | `src/runpod_serverless_payload.py` |
| **Presigned URL** | A web link that works for a set time without a password | `src/s3_video_store.py` |
| **S3 bucket** | A cloud storage container (Amazon) | `src/s3_video_store.py` |
| **SSE-S3** | Server-side encryption managed by Amazon S3 | `src/s3_video_store.py:151-158` |
| **SSH** | A secure way to log in to a remote computer and copy files | `src/runpod_ssh.py` |
| **Jupyter** | A web-based notebook; used here as a file-transfer route to a pod | `src/runpod_jupyter.py` |
| **Proxy** | A middle server that forwards requests | `src/runpod_jupyter.py:196-198` |
| **CI** | Continuous integration: tests run automatically on each change | `.github/workflows/tests.yml` |
| **Lock file** | A list of exact library versions with checksums, so installs repeat | `requirements/*.lock` |
| **Hash pinning** | Checking each downloaded library against a known checksum | `--require-hashes` in `.github/workflows/tests.yml` |

## 17.6 Software and web terms

| Term | Plain meaning | Where it appears |
|---|---|---|
| **Flask** | A small Python web framework | `app.py` |
| **Route / endpoint** | A web address that the app answers, such as `/api/progress/<id>` | `app.py` |
| **Polling** | The browser asking the server "any news?" on a timer | `static/js/app.js:645-688` |
| **Daemon thread** | A background worker thread that stops when the main program stops | `app.py:832-845` |
| **Lock (file or thread)** | A way to make sure only one program or thread changes a file or value at a time | `src/endpoint_capacity.py:42` |
| **Mermaid** | A text format for drawing diagrams, used in this document | `docs/03-architecture.md` |
| **Redaction** | Replacing secret values with a placeholder before text is shown or saved | `src/log_safety.py` |
| **Stub / mock** | A fake stand-in for a real service, used in tests | `tests/` |
| **Node.js** | A JavaScript runtime used here only to run browser-logic tests | `tests/test_*_js.py` |
| **Unit test** | A test of one small piece of code | `tests/` |

## 17.7 Acronyms

| Acronym | Full form | Plain meaning |
|---|---|---|
| ASR | Automatic speech recognition | Speech-to-text by machine |
| API | Application programming interface | A set of web addresses a program can call |
| ASS | Advanced SubStation Alpha | A caption file format |
| CRF | Constant rate factor | A video quality setting (lower is better) |
| CUDA | Compute Unified Device Architecture | NVIDIA GPU programming platform |
| CVE | Common Vulnerabilities and Exposures | A public ID for a security flaw |
| GPU | Graphics processing unit | Chip good at parallel number work; used for AI |
| HTTP | Hypertext Transfer Protocol | How browsers and servers talk |
| JSON | JavaScript Object Notation | A text format for structured data |
| LLM | Large language model | A text-writing AI |
| MP3 / MP4 | MPEG audio layer 3 / MPEG-4 | Audio and video file formats |
| NLLB | No Language Left Behind | Meta's translation model family |
| S3 | Simple Storage Service | Amazon's cloud file storage |
| SABR | Server-side adaptive bitrate streaming (YouTube) | A YouTube delivery method that can block some downloads; named in error messages |
| SRT | SubRip Subtitle | A common subtitle file format |
| SSE | Server-side encryption | Encryption applied by the storage service |
| SSH | Secure Shell | Encrypted remote login |
| TTS | Text to speech | Spoken audio from text |
| UI | User interface | What the user sees |
| URL | Uniform Resource Locator | A web address |
| UTF-8 | Unicode Transformation Format 8-bit | A text encoding that covers all scripts |
| VRAM | Video RAM | Memory on a GPU |

---

# Gap list: `NOT FOUND` items to fill in

These items are missing from the repository. Each needs your input or a decision before it can be written down. Items
marked **owner** need information only you have.

## Owner information

1. **Owner and contact name for the project** (owner). The repository has one git author, `uks`, and no author or contact
   page (`git shortlog`).
2. **Licence** (owner). No `LICENSE` or `COPYING` file exists in the repository.
3. **Target users and personas** (owner). Only notes say "college / demo project" (`docs/AI_HANDOFF_CONTEXT.md:6`).
4. **Intended hosting for the web app** (owner). The app runs on `127.0.0.1` only; no public hosting is in the code.
5. **Public URL or demo link**, if any (owner).

## Product and market

6. **Competitor review and the alternatives you considered** (owner). No comparison exists in the repository.
7. **Success measures and goals with numbers** (owner). Only qualitative goals appear in the notes.
8. **Presentation outline and screenshots** (owner). Screenshots to capture are listed in
   [08-history-limits-results.md](08-history-limits-results.md), section 14.6.

## Performance and results

9. **Benchmark results** for transcription, summarization, highlights and translation (owner or run
   `scripts/run_benchmark_b.py`). None are committed.
10. **Repeated timing measurements** with averages (owner). Only single runs exist (`output/b3848d67/runpod_job_metrics.json`).
11. **Real queue wait time on RunPod** under load (owner, from RunPod console). Needed to judge the S3 signed-link expiry
    (`src/pipeline_executor.py:257`).
12. **Cost per job** (owner, from RunPod billing). Not in the code; the code gives rates only through the RunPod catalogue.
13. **Whisper model cache location** on the laptop (run the app once and check; faster-whisper default, UNCONFIRMED).
14. **Network volume cache location** on the worker (UNCONFIRMED; needed to know where models persist).

## Setup and operations

15. **A command or script that creates `.cache/worker-wheels`** before a Docker build (owner or repo change). Without it,
    a fresh clone cannot build the worker image (`runpod_worker/Dockerfile:20`).
16. **Node.js version** used for the browser-logic tests (owner or CI check).
17. **Linux install steps**, if Linux is a target (owner). CI does not test Linux.
18. **Whether the CI runners have FFmpeg with libass** (check the runner image). Without it the rendering test skips silently.
19. **A complete list of environment variables with defaults**, maintained in one place (owner; the code reads more than `.env.example` lists).
20. **Backup routine and retention policy** for job folders and the selection file (owner). None exists.
21. **S3 lifecycle rule and bucket versioning**, if wanted (owner, AWS console). The retention plan is a proposal only
    (`docs/S3_SOURCE_RETENTION_PLAN.md`).
22. **Production monitoring or alerting**, if wanted (owner). None exists.
23. **Coverage measurement** (owner or add a coverage tool). None exists.

## Security

24. **A decision on authentication** for the web app (owner), if it will ever leave the laptop. None exists (gap S3 in
    [07-setup-security-testing.md](07-setup-security-testing.md)).
25. **Whether the upload filename and the summary, subtitle and audio job IDs need fixing now** (owner decision; gaps S1 and S2).
26. **Whether automatic retries for RunPod gateway errors are wanted** (owner decision; see
    [03-architecture.md](03-architecture.md), decision D8).
27. **Whether the stop and delete-volume buttons should respect leases** (owner decision; gap S4).

## Content that needs writing or checking

28. **Subtitle-gen tool source**, if it is part of the project (owner). It sits outside this repo (`src/transcriber.py:8-19`).
29. **Whether the `scripts/deploy_v103`–`v106` files are kept** (owner decision). They are one-off helpers outside this document's scope.
30. **Whether `translations_compare.md` and `docs/AI_HANDOFF_CONTEXT.md` should be kept in the repository** (owner decision).

## Items deliberately left out

- **Credentials and secrets**: not shown anywhere in this documentation, by design.
- **Endpoint, volume, bucket and pod identifiers**: not reproduced. They are in `data/compute_selection.json`
  (gitignored) and the owner's accounts.
- **The contents of the AWS credential file** named in `.gitignore:27`: never read for this document.
