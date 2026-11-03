FROM python:3.12-slim-bookworm

# util-linux: nsenter (runs zpool, zfs, lsblk and friends on the host) and lsblk
# smartmontools: smartctl, for drive health
RUN apt-get update \
 && apt-get install -y --no-install-recommends util-linux smartmontools \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Its settings, sign-in keys and history; mount these so they survive updates.
VOLUME ["/data", "/config", "/certs"]
EXPOSE 8787

# start-period: getting a Tailscale certificate on first start can take up to 3 minutes
HEALTHCHECK --interval=30s --timeout=10s --start-period=3m --retries=3 \
  CMD ["python", "healthcheck.py"]

CMD ["python", "serve.py"]
