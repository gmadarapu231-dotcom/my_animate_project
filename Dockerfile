# TaxVault production image.
#
# Two stages so the runtime image carries no compiler and no build cache. The
# process runs as a non-root user with a read-only root filesystem in mind:
# nothing is written outside /tmp, because in production the master key comes
# from the environment and the database is PostgreSQL.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY taxvault ./taxvault
COPY careeros ./careeros

# Installed into a virtualenv so the whole tree copies to the runtime stage in
# one layer, with no pip metadata or build tooling along for the ride.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install . "psycopg[binary]>=3.1" "alembic>=1.13" "gunicorn>=21.2"


FROM python:3.12-slim AS runtime

# Reading a photographed W-2 shells out to two programs: Tesseract reads the
# characters and Poppler's pdftoppm renders a scanned PDF into a page image.
# Without them an upload that is pixels rather than text cannot be read at
# all, and most clients photograph the form rather than downloading it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
 && rm -rf /var/lib/apt/lists/*

# A fixed uid so a mounted volume's ownership is predictable across hosts.
RUN groupadd --gid 10001 taxvault \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin taxvault

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=taxvault:taxvault taxvault ./taxvault
COPY --chown=taxvault:taxvault migrations ./migrations
COPY --chown=taxvault:taxvault alembic.ini ./

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TAXVAULT_ENV=production \
    WEB_CONCURRENCY=4

USER taxvault
EXPOSE 8000

# Readiness, not liveness: this is the probe that knows whether the database is
# reachable and the secrets are present.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=4).status==200 else 1)"

# uvicorn workers under gunicorn: gunicorn supervises and restarts a worker
# that dies, which uvicorn alone will not do.
CMD ["gunicorn", "taxvault.api.app:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--forwarded-allow-ips", "*", \
     "--timeout", "60", \
     "--graceful-timeout", "30"]
