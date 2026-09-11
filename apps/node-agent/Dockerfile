FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# docker CLI manages the media stack; ffprobe samples the internal RTSP path
# for FPS/GOP/timestamp health. Neither MediaMTX API nor RTSP are published by
# the production public overlay.
# Keep index/package failures fatal, but bound transient Debian mirror stalls so
# CI cannot spend the entire job budget waiting on an unreachable package host.
# APT's request timeout is supplemented with a whole-command limit.
RUN timeout --signal=TERM --kill-after=10s 120s \
      apt-get \
      -o APT::Update::Error-Mode=any \
      -o Acquire::Retries=4 \
      -o Acquire::http::Timeout=10 \
      -o Acquire::https::Timeout=10 \
      update \
    && timeout --signal=TERM --kill-after=10s 600s \
      apt-get \
      -o Acquire::Retries=4 \
      -o Acquire::http::Timeout=10 \
      -o Acquire::https::Timeout=10 \
      install -y --no-install-recommends \
      docker-cli \
      docker-compose \
      ca-certificates \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/irlight
COPY agent.py node_entrypoint.py ingest_auth_proxy.py relay_client.py supervisor.py ingest_policy.py ingest_quality.py egress_status.py egress_failure_inspect_cli.py ingest_failure_inspect_cli.py media_stack_inspect_cli.py secret_file_inspect_cli.py ./
COPY docker-compose.control.yml ./

CMD ["python3", "-u", "/opt/irlight/node_entrypoint.py"]
