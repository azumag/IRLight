FROM python:3.14-slim AS certs

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Ubuntu's minimal base image does not ship a usable CA bundle. Bootstrap only
# the public root bundle from the already-trusted official Python slim image so
# Canonical mirrors can be contacted over HTTPS; the Ubuntu ca-certificates
# package is then installed normally and owns the final trust store.
COPY --from=certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

# Keep real package/dependency errors fatal while tolerating short-lived mirror
# stalls. Prefer HTTPS on hosted runners, retry bounded transport failures, and
# make any incomplete index refresh fail before package installation begins.
# APT's per-request timeout is not a whole-command deadline, so wrap update and
# install as well; a stalled mirror can never consume the entire CI job budget.
RUN sed -i \
    -e 's|http://archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' \
    -e 's|http://security.ubuntu.com/ubuntu|https://security.ubuntu.com/ubuntu|g' \
    /etc/apt/sources.list.d/ubuntu.sources \
    && echo "apt network policy: https retries=4 request-timeout=10s update-deadline=120s install-deadline=600s update-error-mode=any" \
    && timeout --signal=TERM --kill-after=10s 120s \
       apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=4 -o Acquire::https::Timeout=10 update \
    && timeout --signal=TERM --kill-after=10s 600s \
       apt-get -o Acquire::Retries=4 -o Acquire::https::Timeout=10 install -y --no-install-recommends \
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
