FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/orchestration:/app/ui \
    UPWORK_CONFIG_PATH=/app/config/settings.json \
    UPWORK_OPTIONS_CACHE=/app/config/options_cache.json \
    UPWORK_TOKEN_CACHE_PATH=/app/.secrets/upwork_mcp_tokens.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY orchestration/ /app/orchestration/
COPY ui/ /app/ui/

CMD ["python", "orchestration/job_poller.py", "--daemon"]
