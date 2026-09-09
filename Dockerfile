# rapidocr-onnxruntime needs Python <= 3.12
FROM python:3.12-slim-bookworm AS base

ARG KEEPFRAME_VERSION=0.1.0
LABEL org.opencontainers.image.title="Keepframe" \
      org.opencontainers.image.description="Local Keepframe maker (and optional admin) server" \
      org.opencontainers.image.version="${KEEPFRAME_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Hash-sum mismatches on deb.debian.org show up as "Unable to fetch some archives".
RUN set -eux; \
    printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::http::Pipeline-Depth "0";' \
      'Acquire::http::No-Cache "true";' \
      > /etc/apt/apt.conf.d/80-acquire; \
    apt-get update; \
    apt-get install -y --no-install-recommends --fix-missing \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    || { \
         rm -rf /var/lib/apt/lists/*; \
         apt-get update; \
         apt-get install -y --no-install-recommends \
            ffmpeg \
            libgl1 \
            libglib2.0-0; \
       }; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY keepframe ./keepframe

RUN pip install --no-cache-dir .[ocr] \
    && mkdir -p /data/workspace

WORKDIR /data/workspace
EXPOSE 8765
ENTRYPOINT ["keepframe"]
CMD ["serve", "--workspace", "/data/workspace", "--host", "0.0.0.0", "--port", "8765"]

# CUDA wheels. Host needs nvidia-container-toolkit. Override TORCH_CUDA=cu126 if needed.
FROM base AS gpu
ARG TORCH_CUDA=cu124
RUN pip install --no-cache-dir "torch>=2.2" --index-url "https://download.pytorch.org/whl/${TORCH_CUDA}" \
    && pip uninstall -y onnxruntime \
    && pip install --no-cache-dir onnxruntime-gpu

# Last stage is the default `docker compose build` (CPU).
FROM base AS runtime
