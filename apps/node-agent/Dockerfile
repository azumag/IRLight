FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# docker CLI only: the agent manages the media stack through the read-only
# Docker socket it is given. The socket mount is the only host access granted.
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/irlight
COPY agent.py supervisor.py ingest_policy.py ./

CMD ["python3", "-u", "/opt/irlight/agent.py"]
