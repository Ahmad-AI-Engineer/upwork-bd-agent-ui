# Upwork BD Agent

Polls Upwork's official MCP server for new job postings per configured domain
and writes a daily JSON report. Orchestrated by Apache Airflow.

## Architecture

```
Airflow (api-server + scheduler + dag-processor)  ->  Postgres (metadata)
        |
        +-- DAG: upwork_job_poller  (every 10 min)
              |
              +-- poll_jobs           : find_jobs search + get -> report
              +-- summarize_report    : log per-domain job counts
```

The MCP connection is retried in-process (4 attempts, exponential backoff
starting at 3s) before a task is allowed to fail, because container DNS to
`mcp.upwork.com` occasionally blips. Airflow task-level retries (3, 30s
exponential) sit behind that as a second line of defence.

The poller code is baked into the Airflow image and executed in-process by
`PythonOperator` tasks, so Airflow owns all scheduling. There is no separate
long-running poller container.

## Prerequisites

Authorize Upwork **once on the host** before running in Docker. The OAuth flow
opens a browser, which a container cannot do:

```bash
python src/upwork_connection.py
```

This writes `.secrets/upwork_mcp_tokens.json`, which is mounted into the
containers at runtime (never baked into the image). The MCP SDK refreshes the
token automatically, so no further browser interaction is needed.

## Running with Docker

```bash
docker compose build
docker compose up -d
```

This starts four containers: `postgres`, `airflow-apiserver`, `airflow-scheduler`
and `airflow-dag-processor`. The DB migration runs as part of the apiserver's
startup, so there is no separate init container.

Airflow UI: http://localhost:8080

The login user is `admin`. SimpleAuthManager auto-generates the password:

```bash
docker compose exec airflow-apiserver cat /opt/airflow/simple_auth_manager_passwords.json.generated
```

The DAG is created paused. Unpause it in the UI, or:

```bash
docker compose exec airflow-scheduler airflow dags unpause upwork_job_poller
```

Run it once immediately, without waiting for the schedule:

```bash
docker compose exec airflow-scheduler airflow dags test upwork_job_poller
```

Tear down (add `-v` to also drop the Postgres volume):

```bash
docker compose down
```

## Running without Docker

One-shot (what Airflow does):

```bash
python src/job_poller.py
```

Continuous, self-scheduling every 60s:

```bash
python src/job_poller.py --daemon
```

## Configuration

`config/domains.json`:

```json
{
  "org_uid": "682932163754856448",
  "org_label": "Axiom World (Agency)",
  "domains": [
    { "name": "Power BI", "query": "Power BI", "last_seen_posted_at": null }
  ]
}
```

- `org_uid` — which Upwork identity to search as (from `list_accounts`).
- `domains[]` — one entry per search domain. Add more to poll more domains.
- `last_seen_posted_at` — dedup cursor, updated automatically after each poll.
  Set to `null` or `""` (or omit) to treat the next run as a fresh baseline and
  return the latest jobs unfiltered.

Paths are overridable by environment variable, which is how the containers
point at mounted volumes:

| Variable | Default |
| --- | --- |
| `UPWORK_CONFIG_PATH` | `config/domains.json` |
| `UPWORK_JOBS_DIR` | `jobs/` |
| `UPWORK_LOGS_DIR` | `logs/` |
| `UPWORK_TOKEN_CACHE_PATH` | `.secrets/upwork_mcp_tokens.json` |

## Output

`jobs/upwork_jobs_<YYYY-MM-DD>.json` — one file per day, appended across poll
runs, deduplicated by `job_id`:

```json
{
  "generated_at": "2026-09-02T14:00:20+00:00",
  "source": "Upwork MCP",
  "domains": {
    "Power BI": [
      {
        "required_fields": {
          "job_information": {},
          "contract_information": {},
          "job_requirements": {},
          "client_information": {},
          "application_information": {}
        },
        "additional_mcp_fields": {}
      }
    ]
  }
}
```

`required_fields` holds the fixed reporting schema defined in
`src/job_schema.py`. Fields Upwork's `find_jobs` API does not provide are
present but `null`. `additional_mcp_fields` carries every remaining field the
MCP server returned, so nothing is discarded.

## Connects

`find_jobs` costs no Connects — neither `search` nor `get`. Verified against
real Connects balance and transaction history. The `connects_cost` field in a
`get` response is informational: it is the price of *applying* to that job.
Connects are only spent on actual proposal submission and boosts.

## Modules

| File | Purpose |
| --- | --- |
| `src/upwork_connection.py` | OAuth 2.1 + PKCE, token cache, MCP session |
| `src/job_poller.py` | Poll loop, dedup cursor, report writing |
| `src/job_flatten.py` | Flattens nested MCP responses to dotted-path keys |
| `src/job_schema.py` | Maps flat records to the fixed reporting schema |
| `dags/upwork_job_poller_dag.py` | Airflow DAG definition |
