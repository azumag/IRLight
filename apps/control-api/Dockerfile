FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY apps/control-api/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/control-api/app.py apps/control-api/node_internal.py \
     apps/control-api/catalog_api.py apps/control-api/catalog_store.py \
     apps/control-api/session_api.py apps/control-api/session_store.py \
     apps/control-api/session_workflow.py apps/control-api/reaper.py \
     apps/control-api/fake_provider_for_api.py ./
COPY provider ./provider
COPY apps/control-api/static ./static

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]