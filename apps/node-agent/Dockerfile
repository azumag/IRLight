FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# docker CLI manages the media stack; ffprobe samples the internal RTSP path
# for FPS/GOP/timestamp health. Neither MediaMTX API nor RTSP are published by
# the production public overlay.
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/irlight
COPY agent.py supervisor.py ingest_policy.py ingest_quality.py ./

CMD ["python3", "-u", "/opt/irlight/agent.py"]
