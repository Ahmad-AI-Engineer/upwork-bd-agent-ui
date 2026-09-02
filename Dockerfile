FROM apache/airflow:3.3.1-python3.12

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

USER airflow

COPY requirements-docker.txt /tmp/requirements-docker.txt
RUN pip install --no-cache-dir -r /tmp/requirements-docker.txt

COPY --chown=airflow:root src/ /opt/airflow/upwork/src/
COPY --chown=airflow:root dags/ /opt/airflow/dags/

ENV PYTHONPATH=/opt/airflow/upwork/src \
    UPWORK_CONFIG_PATH=/opt/airflow/upwork/config/domains.json \
    UPWORK_JOBS_DIR=/opt/airflow/upwork/jobs \
    UPWORK_LOGS_DIR=/opt/airflow/upwork/logs \
    UPWORK_TOKEN_CACHE_PATH=/opt/airflow/upwork/.secrets/upwork_mcp_tokens.json
