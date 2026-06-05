FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/models/cache \
    HF_HOME=/models/cache/huggingface \
    TORCH_HOME=/models/cache/torch \
    WHISPER_CACHE_DIR=/models/cache/whisper

WORKDIR /app

ARG INSTALL_AUDIO=true

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential ffmpeg git libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && \
    useradd --system --gid app --home-dir /home/app --shell /usr/sbin/nologin app && \
    mkdir -p /home/app /models/cache

COPY requirements.txt requirements-audio.txt ./
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt && \
    if [ "$INSTALL_AUDIO" = "true" ]; then python -m pip install -r requirements-audio.txt; fi && \
    apt-get purge -y --auto-remove build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY gateway ./gateway
COPY tests ./tests
COPY scripts ./scripts
COPY pytest.ini .
COPY Dockerfile compose.yaml compose.cpu.yaml compose.gpu-amd.yaml compose.gpu-nvidia.yaml ./

RUN chown -R app:app /app /home/app /models

# Allow git to operate on /app even when it is bind-mounted from the host and
# owned by a different uid (common with Docker Desktop on Windows/macOS).
RUN git config --system --add safe.directory /app

USER app
EXPOSE 8080

CMD ["python", "-m", "gateway.main"]
