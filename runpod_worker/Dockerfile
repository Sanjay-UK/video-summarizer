FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl zstd \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://ollama.com/install.sh | sh

COPY runpod_worker/requirements-worker.txt /tmp/requirements-worker.txt
RUN pip install --no-cache-dir -r /tmp/requirements-worker.txt

COPY config.py /app/config.py
COPY src /app/src
COPY runpod_worker/handler.py /app/runpod_worker/handler.py

ENV PYTHONPATH=/app
ENV OLLAMA_HOST=http://127.0.0.1:11434

# Ollama in background; handler is main process
CMD bash -c "ollama serve & sleep 3 && exec python /app/runpod_worker/handler.py"
