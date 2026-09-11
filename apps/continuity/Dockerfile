FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Keep real package/dependency errors fatal while tolerating short-lived mirror
# stalls. The fixed retry/timeout values also bound how long CI waits on a
# degraded archive before surfacing the original apt failure.
RUN echo "apt network policy: retries=3 timeout=30s" \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
    python3 \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY state.py continuity.py control_state.py secret_files.py standby_asset.py runtime_timer_config.py runner.py make_default_standby.py ./

RUN mkdir -p /opt/irlight/assets \
    && python3 /app/make_default_standby.py /opt/irlight/assets/default-standby.png

CMD ["python3", "-u", "/app/runner.py"]
