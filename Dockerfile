# The AE extension, signed: Adobe ZXPSignCmd (Windows build) under Wine, same pins as scripts/zxp/Dockerfile.
# shortcut: a fresh self-signed cert per image build; to keep one publisher identity, sign with scripts/build_zxp.sh
# and mount that keepframe.zxp over /usr/local/lib/python3.12/site-packages/keepframe/ae/static/keepframe.zxp.
FROM scottyhardy/docker-wine@sha256:0952273fae7eada20bd4b8393fcc133082d0ad57cef45d88d524dc6b2d985f18 AS zxp
ADD --chmod=755 --checksum=sha256:6e9d0ce79dd1f766da65acc2130a4388c0b7619a6bcdda71c2eafcccbe118a72 \
  https://raw.githubusercontent.com/Adobe-CEP/CEP-Resources/ab5e4e3e53a42fad08e1225a22a991bb1ffe73f6/ZXPSignCMD/4.1.103/win32/ZXPSignCmd.exe /opt/ZXPSignCmd.exe
ARG KEEPFRAME_VERSION=0.1.0
ENV WINEDEBUG=-all WINEPREFIX=/tmp/wine
COPY extension /src/extension
COPY scripts/zxp/stamp.py /src/stamp.py
RUN set -eu; mkdir -p /stage /out; \
    cp -r /src/extension/CSXS /src/extension/css /src/extension/host /src/extension/js /src/extension/index.html /stage/; \
    KEEPFRAME_BUILD_REV="image-${KEEPFRAME_VERSION}" python3 /src/stamp.py /stage /src; \
    pass=$(head -c 24 /dev/urandom | base64 | tr -d '/+='); \
    wine /opt/ZXPSignCmd.exe -selfSignedCert US CA Keepframe Keepframe "$pass" /tmp/cert.p12 -validityDays 3650 >/dev/null 2>&1; \
    wine /opt/ZXPSignCmd.exe -sign /stage /out/keepframe.zxp /tmp/cert.p12 "$pass" >/dev/null 2>&1; \
    wine /opt/ZXPSignCmd.exe -verify /out/keepframe.zxp 2>/dev/null | grep -q 'Signature verified successfully'

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
        fontconfig fonts-noto-cjk \
        libgl1 \
        libglib2.0-0 \
    || { \
         rm -rf /var/lib/apt/lists/*; \
         apt-get update; \
         apt-get install -y --no-install-recommends \
            ffmpeg \
            fontconfig fonts-noto-cjk \
            libgl1 \
            libglib2.0-0; \
       }; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY keepframe ./keepframe
COPY --from=zxp /out/keepframe.zxp ./keepframe/ae/static/keepframe.zxp

RUN pip install --no-cache-dir ".[ocr,llm]" \
    && python -m playwright install --with-deps chromium \
    && mkdir -p /data/workspace

WORKDIR /data/workspace
EXPOSE 8765 1455
ENTRYPOINT ["keepframe"]
CMD ["serve", "--workspace", "/data/workspace", "--host", "0.0.0.0", "--port", "8765"]

# CUDA wheels. Host needs nvidia-container-toolkit. Override TORCH_CUDA=cu126 if needed.
FROM base AS gpu
ARG TORCH_CUDA=cu124
RUN pip install --no-cache-dir "torch>=2.2" --index-url "https://download.pytorch.org/whl/${TORCH_CUDA}" \
    && pip uninstall -y onnxruntime onnxruntime-gpu \
    && pip install --no-cache-dir "onnxruntime-gpu>=1.19,<1.27"

# Last stage is the default `docker compose build` (CPU).
FROM base AS runtime
