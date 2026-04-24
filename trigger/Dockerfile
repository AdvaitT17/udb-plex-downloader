FROM python:3.11-slim

# ffmpeg for UDB; build toolchain for native wheels (quickjs) on ARM
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
        build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install UDB deps + trigger deps
COPY requirements.txt /app/requirements.txt
COPY trigger/requirements.txt /app/trigger/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/trigger/requirements.txt \
    && apt-get purge -y --auto-remove build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy UDB source + trigger
COPY . /app

# Writable dirs for queue + logs + downloads
RUN mkdir -p /app/trigger/data/logs /downloads

ENV UDB_ROOT=/app \
    UDB_CONFIG=/app/config_udb.yaml \
    UDB_TRIGGER_DATA=/app/trigger/data \
    UDB_TRIGGER_LOGS=/app/trigger/data/logs \
    UDB_TRIGGER_DB=/app/trigger/data/queue.sqlite \
    UDB_PYTHON=python3 \
    PYTHONUNBUFFERED=1

EXPOSE 8787

CMD ["uvicorn", "trigger.server:app", "--host", "0.0.0.0", "--port", "8787"]
