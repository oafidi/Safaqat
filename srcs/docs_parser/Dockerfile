# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# paddlepaddle-gpu 3.1.0 is not on PyPI, so it comes from Paddle's index.
# curl, not pip: the wheel is 1.7 GB from a CDN that stalls, and curl can resume
# (-C -) where pip would time out and discard the whole download.
ARG PADDLE_WHEEL=paddlepaddle_gpu-3.1.0-cp312-cp312-linux_x86_64.whl
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/wheels \
    set -eu; \
    if [ ! -f "/wheels/${PADDLE_WHEEL}.done" ]; then \
        curl -fL --retry 20 --retry-delay 5 --retry-all-errors -C - \
            -o "/wheels/${PADDLE_WHEEL}" \
            "https://paddle-whl.bj.bcebos.com/stable/cu123/paddlepaddle-gpu/${PADDLE_WHEEL}"; \
        touch "/wheels/${PADDLE_WHEEL}.done"; \
    fi; \
    pip install --timeout 120 --retries 10 "/wheels/${PADDLE_WHEEL}"

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements.txt

COPY src/ src/
COPY service/ service/

ENV HF_HOME=/models/huggingface \
    PADDLE_PDX_CACHE_HOME=/models/paddlex \
    TOKENIZERS_PARALLELISM=false

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /models \
    && ln -s /models/paddlex /home/app/.paddlex \
    && chown -R app:app /models /app /home/app
USER app
VOLUME ["/models"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=5m --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "75"]
