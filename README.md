# Upwork BD Agent

Polls Upwork's official MCP server for new job postings per configured domain
and writes a daily JSON report.

## Architecture

```
orchestration/   poller     loop: reload config -> find_jobs -> jobs/*.json -> POST to Odoo -> sleep
ui/              config-ui  :8000  edit domains, filters and interval
```

`orchestration/` holds the pipeline, `ui/` holds the configuration front end.
Both are on `PYTHONPATH`, so the UI can import the shared filter schema.

Two containers, no database, no orchestrator. The poller is a plain interval
loop; the config UI writes the JSON file the poller reads. Both mount the same
`config/`, `jobs/` and `.secrets/` directories, so a saved change is picked up
on the next poll with no restart.

The MCP connection is retried in-process (4 attempts, exponential backoff from
3s) because container DNS to `mcp.upwork.com` occasionally blips. Logs go to
stdout only — read them with `docker compose logs -f poller`.

## Prerequisites

Authorize Upwork **once on the host** before running in Docker. The OAuth flow
opens a browser, which a container cannot do:

```bash
python orchestration/upwork_connection_handler.py
```

This writes `.secrets/upwork_mcp_tokens.json`, which is mounted into the
containers at runtime (never baked into the image).

### Token refresh

Upwork access tokens last 24 hours. Refresh is automatic and needs no browser,
but two SDK behaviours had to be worked around in `upwork_connection_handler.py`:

1. **Expiry is not persisted by the SDK.** `_initialize()` reloads the token but
   not `token_expiry_time`, so `is_token_valid()` returned true for any stored
   token however old — the proactive refresh never fired, the dead token was
   sent, and the resulting 401 went straight to a full browser authorization.
   `FileTokenStorage` now stores an absolute `expires_at` and blanks the access
   token once it is within 5 minutes of expiry, which makes the SDK take the
   refresh path it already implements.
2. **The refresh went to the wrong URL.** Without discovered metadata the SDK
   falls back to `urljoin(server_url, "/token")` — `https://mcp.upwork.com/token`,
   which 404s. `prime_oauth_metadata()` loads the well-known document up front so
   the real endpoint (`https://www.upwork.com/api/v3/oauth2/token`) is used.

If refresh genuinely fails, containers fail fast instead of hanging:
`UPWORK_HEADLESS=1` (set in compose) makes the poller raise immediately with an
instruction to re-authorize on the host, then wait for the next interval.

## Running with Docker

```bash
docker compose build
docker compose up -d
```

Two containers start: `poller` and `config-ui`.

- Config UI: http://localhost:8000
- Logs: `docker compose logs -f poller`

Run one poll immediately, without waiting for the interval:

```bash
docker compose exec poller python orchestration/job_poller.py
```

Tear down:

```bash
docker compose down
```

## Running without Docker

Everything at once — poller loop plus config UI in a single process:

```bash
python test_run.py
```

Config UI on http://localhost:8000, Ctrl+C stops both. This is a local
convenience entrypoint only; in Docker the poller and UI run as separate
containers, so `test_run.py` is not used there and can be deleted on deploy.

### Individual pieces

One-shot — poll once and exit:

```bash
python orchestration/job_poller.py
```

Continuous — the loop the container runs:

```bash
python orchestration/job_poller.py --daemon
```

Config UI on the host (needs `orchestration/` on the path for the shared schema):

```bash
PYTHONPATH=orchestration uvicorn config_ui:app --app-dir ui --port 8000
```

## Configuration

Edit it in the browser at **http://localhost:8000**, or by hand:

```json
{
  "org_uid": "682932163754856448",
  "org_label": "Axiom World (Agency)",
  "domains": [
    {
      "enabled": true,
      "filters": {
        "query": "Power BI",
        "limit": 10,
        "sort": "recency",
        "job_type": "fixed",
        "experience_level": "expert",
        "budget_min": 100,
        "proposals_max": 20,
        "verified_payment_only": true,
        "skills": ["Microsoft Power BI"]
      },
      "last_seen_posted_at": null
    }
  ]
}
```

- `org_uid` — which Upwork identity to search as (from `list_accounts`).
- `poll_interval_minutes` — how often the loop runs; reloaded every cycle.
- `filters.query` — the search string, and the domain's identity: it is the
  bucket key in the report and what the timestamp is tracked against. Two
  domains cannot share a query.
- `enabled` — set false to pause a domain without deleting it.
- `filters` — passed to `find_jobs`. Every field is defined once in
  `orchestration/search_filters.py`, which also generates the UI form, so the two cannot
  drift. Omitted/blank fields are simply not sent.
- `last_seen_posted_at` — the newest job timestamp already seen; each poll only
  reports jobs posted after it. Updated automatically after every poll. Set to
  `null` or `""` (or omit), or tick **Reset timestamp** in the UI, to take a
  fresh baseline on the next poll — useful after changing a domain's filters,
  since jobs that only match the new filters were otherwise already skipped.

The poller writes back only `last_seen_posted_at`, merged into the file as it is
on disk, so a change saved in the UI during a poll is never overwritten.

The legacy shape (`"query"` at the top level, no `filters`) is still accepted.

### Filter reference

| Group | Fields |
| --- | --- |
| Search | `query`, `sort`, `limit` (1–10), `category`, `subcategory`, `skills` (max 5) |
| Contract | `job_type`, `experience_level`, `budget_min/max` (fixed only), `rate_min/max` (hourly only), `workload`, `duration` |
| Competition | `proposals_min/max` |
| Client | `verified_payment_only`, `previous_clients_only`, `client_hires_min/max`, `timezone`, `location` |

Three API constraints the UI enforces: `limit` caps at 10, `skills` accepts at
most 5 and must match Upwork's skill names exactly, and `budget_*` (fixed-price)
is not interchangeable with `rate_*` (hourly).

Paths are overridable by environment variable, which is how the containers
point at mounted volumes:

| Variable | Default |
| --- | --- |
| `UPWORK_CONFIG_PATH` | `config/settings.json` |
| `UPWORK_JOBS_DIR` | `jobs/` |
| `UPWORK_OPTIONS_CACHE` | `config/options_cache.json` |
| `UPWORK_TOKEN_CACHE_PATH` | `.secrets/upwork_mcp_tokens.json` |
| `UPWORK_HEADLESS` | unset (set to `1` in containers) |

## CRM push

Every poll POSTs the jobs it just appended to the Odoo webhook set in
`crm_endpoint`. Set `crm_enabled: false` to collect locally without pushing.

The route is an Odoo `type="json"` controller, so it requires a JSON-RPC 2.0
envelope; a bare array is rejected with `400 Invalid JSON-RPC data`. The jobs
array sits under `params`, because Odoo expands `params` into the handler's
keyword arguments:

```json
{"jsonrpc": "2.0", "method": "call",
 "params": {"jobs": [ {...}, {...} ]}, "id": null}
```

- `crm_params_key` changes the key inside `params` (default `jobs`).
- `crm_payload_style: "raw"` sends a bare array instead, for a plain HTTP endpoint.
- `UPWORK_CRM_ENDPOINT` overrides `crm_endpoint` from the environment.

`orchestration/crm_payload.py` converts our record into the flat 50-field object Odoo expects.
The conversions are not cosmetic: `skills` becomes a comma-joined string,
`client_total_spent` `"$3,356.99"` becomes `3356.99`, `client_payment_verified`
`"VERIFIED"` becomes `true`, `job_type` is upper-cased, timestamps become
`.sssZ`, and the `<untrusted_participant_content>` wrapper is stripped from
descriptions.

About half the fields are `null` on most jobs (`category`, `occupation`,
`client_city`, `client_industry`, `client_member_since`, `last_client_activity`
and others) because Upwork's `find_jobs` does not return them. Client stats are
also absent for brand-new clients, and hourly rate bounds are absent when the
client accepted Upwork's default range. Anything `required=True` on the Odoo
side will therefore reject records.

Failures retry three times with backoff, then log loudly. Jobs are always
written to the daily report first, so a failed batch can be replayed:

```bash
docker compose exec poller python orchestration/crm_client.py jobs/upwork_jobs_2026-09-03.json
```

## Implementation notes

Behaviour that is deliberate and easy to undo by accident:

- **The token cache stores an absolute `expires_at` and blanks the access token
  near expiry.** The SDK reloads tokens without restoring `token_expiry_time`,
  so it would treat any stored token as valid, send a dead one, and fall through
  to a browser authorization on the 401 instead of refreshing.
- **OAuth metadata is fetched before the first request.** Without it the SDK
  posts refreshes to `urljoin(server_url, "/token")`, which 404s; the real
  endpoint is on `www.upwork.com`.
- **The poller writes back only `last_seen_posted_at`**, merged into the file as
  it is on disk. Writing the whole config would discard edits saved in the UI
  while a poll was running.
- **The config UI merges into the existing file** rather than rebuilding it, so
  top-level keys the form does not manage (`crm_endpoint`, `crm_enabled`, ...)
  survive a save.
- **`job_flatten` JSON-encodes lists of dicts.** Their shapes vary between jobs
  and Polars' strict struct inference rejects the mismatch.

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
`orchestration/job_schema.py`. Fields Upwork's `find_jobs` API does not provide are
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
| `orchestration/upwork_connection_handler.py` | OAuth 2.1 + PKCE, token cache, MCP session |
| `orchestration/job_poller.py` | Poll loop, dedup cursor, report writing |
| `orchestration/job_flatten.py` | Flattens nested MCP responses to dotted-path keys |
| `orchestration/job_schema.py` | Maps flat records to the fixed reporting schema |
| `orchestration/search_filters.py` | Filter schema — drives both the API call and the UI form |
| `ui/config_ui.py` | FastAPI config editor (reads/writes `config/settings.json`) |
| `ui/config_ui.html` | Config UI front end |
| `orchestration/options_source.py` | Discovers valid category/subcategory lists from Upwork |
| `orchestration/crm_payload.py` | Converts a report record to the Odoo webhook's flat schema |
| `orchestration/crm_client.py` | Posts jobs to the webhook; also a CLI to replay a report |
