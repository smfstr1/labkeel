# LabKeel, for amd64 and arm64.  Build and start:  docker compose up -d --build
# Python 3.13 on Debian 13 (trixie), in step with the versions the tests run on (see .github/workflows/ci.yml).
FROM python:3.13-slim-trixie

LABEL org.opencontainers.image.title="LabKeel" \
      org.opencontainers.image.description="A dashboard for running your home server: containers, drives, ZFS, files and alerts" \
      org.opencontainers.image.source="https://github.com/smfstr1/labkeel"

# util-linux: nsenter (runs zpool, zfs, lsblk and friends on the host) and lsblk
# smartmontools: smartctl, for drive health
RUN apt-get update \
 && apt-get install -y --no-install-recommends util-linux smartmontools \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ .
COPY VERSION .
# compiled once here, so start-up doesn't (the app never writes .pyc files at run time)
RUN python -m compileall -q .

# It runs as root on purpose: reading drive health, ZFS and the host's processes needs it, and so does the
# Docker socket (see the compose file). Who may do what is decided inside LabKeel, by accounts and roles.

# Its accounts, settings, database, sign-in keys and certificates: mount these so they survive updates.
VOLUME ["/data", "/config", "/certs"]
EXPOSE 8787
STOPSIGNAL SIGTERM

# start-period: getting a Tailscale certificate on first start can take up to 3 minutes
HEALTHCHECK --interval=30s --timeout=10s --start-period=3m --retries=3 \
  CMD ["python", "healthcheck.py"]

CMD ["python", "serve.py"]
