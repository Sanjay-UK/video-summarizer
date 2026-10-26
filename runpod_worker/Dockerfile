FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app

# FFmpeg (libavfilter/subtitles for highlights) — baked at build time, not fetched at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends curl zstd ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -n 1 \
    && curl -fsSL https://ollama.com/install.sh | sh

# Worker dependencies come from the hashed lock (requirements/worker.lock), installed OFFLINE from the wheel
# cache (.cache/worker-wheels, built from the same lock). PyPI downloads from the build VM have stalled repeatedly.
# Torch is NOT in the lock: the runpod/pytorch base image provides CUDA torch 2.4.1+cu124, and --no-index
# stops pip from replacing it. transformers 4.49.0 is pinned because 4.50+ refuses torch.load on torch<2.6
# (CVE-2025-32434). Keep worker.lock in step with requirements/worker.in.
# gtts is installed separately with --no-deps: its click<8.2 cap conflicts with runpod's click>=8.4.2,
# and gtts only needs click for its command-line tool, not the library used for highlight narration.
COPY requirements/worker.lock /tmp/worker.lock
COPY requirements/worker-gtts.lock /tmp/worker-gtts.lock
COPY .cache/worker-wheels /tmp/wheels
RUN pip install --no-index --find-links /tmp/wheels --require-hashes -r /tmp/worker.lock \
    && pip install --no-index --find-links /tmp/wheels --require-hashes --no-deps -r /tmp/worker-gtts.lock \
    && rm -rf /tmp/wheels /tmp/worker.lock /tmp/worker-gtts.lock \
    && python -c "import torch, transformers, tokenizers, huggingface_hub; from transformers.utils.import_utils import is_torch_available; from transformers import AutoModelForSeq2SeqLM; assert torch.__version__.startswith('2.4.1'), torch.__version__; assert transformers.__version__ == '4.49.0', transformers.__version__; assert is_torch_available(); print('torch', torch.__version__, 'cuda build', torch.version.cuda, 'transformers', transformers.__version__, 'tokenizers', tokenizers.__version__, 'hub', huggingface_hub.__version__)"

COPY config.py /app/config.py
COPY src /app/src
# Bundled Unicode fonts for highlight captions (Devanagari, Kannada, Tamil, Telugu, Latin).
# Without them libass draws empty boxes for non-Latin text, because the base image has no Indic fonts.
COPY static/fonts /app/static/fonts
# Register the same fonts with fontconfig, so libass can fall back to them for mixed-script lines.
COPY static/fonts /usr/share/fonts/truetype/video-summarizer/
RUN fc-cache -f
COPY runpod_worker/handler.py /app/runpod_worker/handler.py

ENV PYTHONPATH=/app
ENV OLLAMA_HOST=http://127.0.0.1:11434

# Ollama in background; handler is main process
CMD bash -c "ollama serve & sleep 3 && exec python /app/runpod_worker/handler.py"
