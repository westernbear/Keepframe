# rapidocr-onnxruntime needs Python <= 3.12
FROM python:3.12-slim-bookworm

ARG KEEPFRAME_VERSION=0.1.0
LABEL org.opencontainers.image.title="Keepframe" \
      org.opencontainers.image.description="Local Keepframe maker (and optional admin) server" \
      org.opencontainers.image.version="${KEEPFRAME_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY refstudio ./refstudio

RUN pip install --no-cache-dir .[ocr] \
    && mkdir -p /data/workspace

WORKDIR /data/workspace
EXPOSE 8765
ENTRYPOINT ["keepframe"]
CMD ["serve", "--workspace", "/data/workspace", "--host", "0.0.0.0", "--port", "8765"]
