FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates srt-tools \
    && rm -rf /var/lib/apt/lists/*
COPY apps/control-api/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/control-api/app.py apps/control-api/node_internal.py \
     apps/control-api/auth_api.py apps/control-api/auth_store.py \
     apps/control-api/catalog_api.py apps/control-api/catalog_store.py \
     apps/control-api/destination_probe.py apps/control-api/destination_url_safety.py \
     apps/control-api/destination_secret_store.py \
     apps/control-api/egress_destination.py apps/control-api/entitlement_store.py \
     apps/control-api/ingest_api.py apps/control-api/ingest_auth_guard.py \
     apps/control-api/ingest_store.py apps/control-api/pipeline_health.py \
     apps/control-api/control_store.py \
     apps/control-api/session_api.py apps/control-api/session_store.py \
     apps/control-api/session_workflow.py apps/control-api/state_safety.py \
     apps/control-api/reaper.py \
     apps/control-api/reaper_cli.py apps/control-api/fake_provider_for_api.py ./
COPY provider ./provider
COPY apps/control-api/static ./static

HEALTHCHECK --interval=15s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()" || exit 1

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
