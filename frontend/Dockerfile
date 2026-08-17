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
    TMPDIR=/var/tmp/pip-tmp

WORKDIR /app

COPY frontend/requirements.txt /tmp/requirements.txt
RUN mkdir -p "$TMPDIR" \
    && python -m pip install --no-cache-dir --upgrade "pip==25.0.1" setuptools wheel \
    && for attempt in 1 2 3; do \
        python -m pip install --no-cache-dir --progress-bar off --prefer-binary -r /tmp/requirements.txt \
        && break; \
        status=$?; \
        echo "pip install failed (attempt ${attempt}/3, exit ${status}); retrying with a clean temp/cache directory"; \
        rm -rf "$TMPDIR"/* /root/.cache/pip; \
        if [ "$attempt" = "3" ]; then exit "$status"; fi; \
        sleep $((attempt * 10)); \
    done \
    && rm -rf "$TMPDIR"

COPY shared /app/shared
COPY frontend /app/frontend
# Frontend only needs the SQLAlchemy ORM models from scrapers/, not the scraper code itself
COPY scrapers/__init__.py /app/scrapers/__init__.py
COPY scrapers/models.py /app/scrapers/models.py
COPY scrapers/models_production.py /app/scrapers/models_production.py

RUN mkdir -p /app/db/images /app/cache /app/logs

CMD ["python", "-m", "frontend"]
