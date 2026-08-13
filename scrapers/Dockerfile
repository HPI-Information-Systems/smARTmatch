FROM python:3.12.7-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_INDEX_URL=https://pypi.org/simple \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libjpeg62-turbo supervisor xvfb \
    && rm -rf /var/lib/apt/lists/*

# Build-time only: keep pip's scratch dir scoped to this RUN so it can't leak
# into runtime (Playwright otherwise tries to mkdtemp inside a deleted path).
COPY requirements.txt /tmp/requirements.txt
RUN PIP_TMPDIR=/var/tmp/pip-tmp \
    && mkdir -p "$PIP_TMPDIR" \
    && TMPDIR="$PIP_TMPDIR" python -m pip install --no-cache-dir --upgrade "pip==25.0.1" setuptools wheel \
    && for attempt in 1 2 3; do \
        TMPDIR="$PIP_TMPDIR" python -m pip install --no-cache-dir --progress-bar off --prefer-binary -r /tmp/requirements.txt \
        && break; \
        status=$?; \
        echo "pip install failed (attempt ${attempt}/3, exit ${status}); retrying with a clean temp/cache directory"; \
        rm -rf "$PIP_TMPDIR"/* /root/.cache/pip; \
        if [ "$attempt" = "3" ]; then exit "$status"; fi; \
        sleep $((attempt * 10)); \
    done \
    && rm -rf "$PIP_TMPDIR"

# Install Playwright's Chromium + system deps required by Lottissimo/Dorotheum.
RUN python -m playwright install --with-deps chromium

COPY shared /app/shared
COPY scrapers /app/scrapers
COPY scraper_dashboard /app/scraper_dashboard

RUN mkdir -p /app/db/images /app/logs

EXPOSE 5555

CMD ["supervisord", "-n", "-c", "/app/scrapers/supervisord.conf"]
