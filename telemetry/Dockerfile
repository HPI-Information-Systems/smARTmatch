FROM python:3.12.7-slim@sha256:60d9996b6a8a3689d36db740b49f4327be3be09a21122bd02fb8895abb38b50d AS dependencies

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

COPY telemetry/requirements.txt /tmp/telemetry-requirements.txt
RUN mkdir -p "$TMPDIR" \
    && python -m pip install --no-cache-dir --upgrade \
        "pip==25.0.1" "setuptools==80.9.0" "wheel==0.46.3" \
    && for attempt in 1 2 3; do \
        python -m pip install --no-cache-dir --progress-bar off --prefer-binary \
            -r /tmp/telemetry-requirements.txt \
        && break; \
        status=$?; \
        rm -rf "$TMPDIR"/* /root/.cache/pip; \
        if [ "$attempt" = "3" ]; then exit "$status"; fi; \
        sleep $((attempt * 10)); \
    done \
    && rm -rf "$TMPDIR"

FROM dependencies AS application

# These component files are source/lock inspection inputs only. Their heavyweight
# requirements are deliberately not installed in the telemetry image.
COPY requirements.txt /app/requirements.txt
COPY matching_pipeline /app/matching_pipeline
COPY scripts/run_pipeline_scheduler.py /app/scripts/run_pipeline_scheduler.py
COPY shared /app/shared
COPY telemetry /app/telemetry

RUN mkdir -p /app/db/images /app/logs

# Resolve HEAD from the minimal Git reference metadata admitted to the build
# context and combine it with a hash of the exact source already copied. Raw
# reference files remain confined to this throwaway stage.
FROM application AS provenance
COPY .git /tmp/repository/.git
RUN python -m telemetry.build_provenance \
    --app-root /app \
    --git-dir /tmp/repository/.git \
    --output /tmp/telemetry-build-provenance.json

FROM application AS final

ENV SMARTMATCH_BUILD_PROVENANCE_FILE=/app/telemetry-build-provenance.json \
    SMARTMATCH_REQUIRE_BUILD_PROVENANCE=true
COPY --from=provenance /tmp/telemetry-build-provenance.json \
    /app/telemetry-build-provenance.json

CMD ["python", "-m", "telemetry.telemetry", "--daemon"]
