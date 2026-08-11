FROM python:3.12.7-slim@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_INDEX_URL=https://pypi.org/simple \
    TMPDIR=/var/tmp/pip-tmp \
    CC=/usr/bin/gcc \
    CXX=/usr/bin/g++

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        libglib2.0-0 \
        libgomp1 \
        libjpeg62-turbo \
        libxcb1 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY matching_pipeline/requirements.txt /tmp/requirements.txt
RUN mkdir -p "$TMPDIR" \
    && python -m pip install --no-cache-dir --upgrade "pip==25.0.1" "setuptools==80.9.0" "wheel==0.46.3" \
    && for attempt in 1 2 3 4 5; do \
        python -m pip install --no-cache-dir --progress-bar off --prefer-binary -r /tmp/requirements.txt \
        && break; \
        status=$?; \
        echo "pip install failed (attempt ${attempt}/5, exit ${status}); retrying with a clean temp/cache directory"; \
        rm -rf "$TMPDIR"/* /root/.cache/pip; \
        if [ "$attempt" = "5" ]; then exit "$status"; fi; \
        sleep $((attempt * 10)); \
    done \
    && for attempt in 1 2 3; do \
        python -m pip install --no-cache-dir --progress-bar off --no-deps \
            "git+https://github.com/cvg/LightGlue@eb42fee2d71449efb0aa5c10549752b5d75384d8" \
        && break; \
        status=$?; \
        if [ "$attempt" = "3" ]; then exit "$status"; fi; \
        sleep $((attempt * 10)); \
    done \
    && rm -rf "$TMPDIR"

COPY matching_pipeline /app/matching_pipeline
COPY scripts/run_pipeline_scheduler.py /app/scripts/run_pipeline_scheduler.py

RUN mkdir -p /app/db/images /app/cache /root/.cache

CMD ["python", "/app/scripts/run_pipeline_scheduler.py", "--interval-minutes", "1"]
