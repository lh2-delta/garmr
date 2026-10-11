# Garmr: completeness checker for migration-downloader exports
# Manual hardening step: pin the base image by digest (python:3.12-slim@sha256:<digest>) once you have looked the
# digest up with network access. It is deliberately not pinned here: a digest cannot be verified offline.
FROM python:3.12-slim
LABEL org.opencontainers.image.title="garmr" \
      org.opencontainers.image.description="Completeness checker for migration-downloader exports" \
      org.opencontainers.image.source="local"

# Retries/timeouts keep an offline build failing within about a minute instead of hanging.
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
 && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends git tar ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# scc measures lines of code for the org-CSV LOC floor. Garmr refuses to run without it, so a failed install
# FAILS THE BUILD (no silent fallback). The download is retried. On a flaky or offline network, put
# scc_Linux_x86_64.tar.gz from https://github.com/boyter/scc/releases into ./vendor/ and it is used instead.
ARG SCC_VERSION=3.7.0
COPY Dockerfile vendor* /tmp/vendor/
RUN set -eu; \
    if [ -f /tmp/vendor/scc_Linux_x86_64.tar.gz ]; then cp /tmp/vendor/scc_Linux_x86_64.tar.gz /tmp/scc.tgz; \
    else curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors -o /tmp/scc.tgz \
         "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/scc_Linux_x86_64.tar.gz"; fi; \
    tar -xzf /tmp/scc.tgz -C /usr/local/bin scc; \
    scc --version; \
    rm -rf /tmp/scc.tgz /tmp/vendor; \
    apt-get purge -y curl; apt-get autoremove -y

WORKDIR /app
COPY check_export.py batch.py /app/
COPY garmr /app/garmr

# Folders are fixed; docker-compose.yml mounts ./input, ./output and ./work onto them.
ENV EXPORT_CHECK_INPUT=/data/input \
    EXPORT_CHECK_OUTPUT=/data/output \
    EXPORT_CHECK_WORK=/data/work \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd -m -u 10001 checker && mkdir -p /data/input /data/output /data/work && chown -R checker /data

# Fingerprint of the sources this image was built from (run.sh compares it to rebuild a stale image). Kept last so it
# does not invalidate the cached apt/scc layers.
ARG GARMR_SRC_HASH=unknown
LABEL garmr.src-hash="${GARMR_SRC_HASH}"
USER checker
ENTRYPOINT ["python", "/app/batch.py"]
