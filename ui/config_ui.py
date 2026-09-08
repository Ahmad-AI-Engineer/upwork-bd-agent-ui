from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import env_bootstrap  # noqa: F401
from options_source import load_options
from search_filters import FILTER_SCHEMA, validate_filters

logger = logging.getLogger("config_ui")

_BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("UPWORK_CONFIG_PATH", _BASE_DIR / "config" / "settings.json"))

ACCOUNTS = [
    {"org_uid": "682932163754856448", "label": "Axiom World (Agency)"},
    {"org_uid": "1113381384263086081", "label": "Kashif Manzoor (Freelancer)"},
]

app = FastAPI(title="Upwork Bid Agent Config")


POLL_INTERVAL_CHOICES = [2, 5, 10, 15, 30, 60, 120, 240]


class ConfigPayload(BaseModel):
    org_uid: str
    org_label: str | None = None
    poll_interval_minutes: int = 10
    domains: list[dict[str, Any]]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {
            "org_uid": ACCOUNTS[0]["org_uid"],
            "org_label": ACCOUNTS[0]["label"],
            "poll_interval_minutes": 10,
            "domains": [],
        }
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


@app.get("/api/schema")
def get_schema() -> dict:
    return {
        "groups": FILTER_SCHEMA,
        "accounts": ACCOUNTS,
        "options": load_options(),
        "poll_intervals": POLL_INTERVAL_CHOICES,
    }


@app.get("/api/config")
def get_config() -> dict:
    return load_config()


@app.post("/api/config")
def post_config(payload: ConfigPayload) -> JSONResponse:
    errors: list[str] = []
    queries = set()
    for d in payload.domains:
        filters = d.get("filters") or {}
        query = (filters.get("query") or "").strip()
        if not query:
            errors.append("every search query needs a value")
            continue
        if query in queries:
            errors.append(f"duplicate search query: {query}")
        queries.add(query)
        for err in validate_filters(filters):
            errors.append(f"{query}: {err}")

    if payload.poll_interval_minutes not in POLL_INTERVAL_CHOICES:
        errors.append(f"poll interval must be one of {POLL_INTERVAL_CHOICES}")

    if errors:
        raise HTTPException(status_code=400, detail=errors)

    existing = {}
    for d in load_config().get("domains", []):
        key = ((d.get("filters") or {}).get("query") or d.get("query") or d.get("name") or "").strip()
        existing[key] = d

    domains = []
    for d in payload.domains:
        filters_in = d.get("filters") or {}
        query = (filters_in.get("query") or "").strip()
        last_seen = existing.get(query, {}).get("last_seen_posted_at")
        if d.get("reset_timestamp"):
            last_seen = None
        domains.append({
            "enabled": bool(d.get("enabled", True)),
            "filters": {k: v for k, v in filters_in.items() if v not in (None, "", [])},
            "last_seen_posted_at": last_seen,
        })

    label = next((a["label"] for a in ACCOUNTS if a["org_uid"] == payload.org_uid), payload.org_label)

    config = load_config()
    config.update({
        "org_uid": payload.org_uid,
        "org_label": label,
        "poll_interval_minutes": payload.poll_interval_minutes,
        "domains": domains,
    })
    save_config(config)
    logger.info(
        "Config saved: %d search quer%s, poll every %d min",
        len(domains),
        "y" if len(domains) == 1 else "ies",
        payload.poll_interval_minutes,
    )
    return JSONResponse({"status": "ok", "domains": len(domains)})


@app.post("/api/reset-timestamps")
def reset_timestamps() -> JSONResponse:
    config = load_config()
    domains = config.get("domains", [])
    for domain in domains:
        domain["last_seen_posted_at"] = None
    save_config(config)
    logger.info("Reset the timestamp on %d domain(s)", len(domains))
    return JSONResponse({"status": "ok", "reset": len(domains)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "config_path": str(CONFIG_PATH), "exists": CONFIG_PATH.exists()}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).resolve().parent / "config_ui.html").read_text(encoding="utf-8")
