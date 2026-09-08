from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("options_source")

_BASE_DIR = Path(__file__).resolve().parent.parent
OPTIONS_CACHE_PATH = Path(
    os.getenv("UPWORK_OPTIONS_CACHE", _BASE_DIR / "config" / "options_cache.json")
)

COMMON_TIMEZONES = [
    "America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York",
    "America/Sao_Paulo", "Europe/London", "Europe/Dublin", "Europe/Lisbon",
    "Europe/Madrid", "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam",
    "Europe/Warsaw", "Europe/Kyiv", "Europe/Istanbul", "Europe/Moscow",
    "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata", "Asia/Dhaka", "Asia/Bangkok",
    "Asia/Singapore", "Asia/Manila", "Asia/Hong_Kong", "Asia/Shanghai",
    "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland",
]

COMMON_LOCATIONS = [
    "United States", "United Kingdom", "Canada", "Australia", "New Zealand",
    "Ireland", "Germany", "France", "Netherlands", "Belgium", "Switzerland",
    "Sweden", "Norway", "Denmark", "Spain", "Italy", "Portugal", "Poland",
    "Ukraine", "United Arab Emirates", "Saudi Arabia", "Qatar", "Israel",
    "India", "Pakistan", "Bangladesh", "Philippines", "Singapore", "Malaysia",
    "Japan", "South Korea", "China", "Brazil", "Mexico", "Argentina", "South Africa",
]

DEFAULT_OPTIONS: dict[str, list[str]] = {
    "categories": [],
    "subcategories": [],
    "skills": [],
    "timezones": COMMON_TIMEZONES,
    "locations": COMMON_LOCATIONS,
}


def load_options() -> dict[str, list[str]]:
    options = dict(DEFAULT_OPTIONS)
    if OPTIONS_CACHE_PATH.exists():
        try:
            cached = json.loads(OPTIONS_CACHE_PATH.read_text(encoding="utf-8"))
            for key, values in cached.items():
                if values:
                    options[key] = values
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read options cache at %s", OPTIONS_CACHE_PATH)
    return options


def save_options(options: dict[str, list[str]]) -> None:
    OPTIONS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPTIONS_CACHE_PATH.write_text(json.dumps(options, indent=2), encoding="utf-8")


def _extract_list(text: str) -> list[str]:
    for pattern in (r"\[([^\]]+)\]", r"(?:valid|allowed|expected)[^:]*:\s*(.+)"):
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            raw = m.group(1)
            parts = [p.strip().strip("'\"") for p in re.split(r"[,\n]", raw)]
            values = [p for p in parts if p and len(p) < 80]
            if len(values) > 1:
                return sorted(set(values))
    return []


async def discover_options(session, org_uid: str) -> dict[str, list[str]]:
    from job_poller import call_find_jobs

    options = dict(DEFAULT_OPTIONS)

    probes = {
        "categories": {"query": "test", "limit": 1, "category": "__invalid__"},
        "subcategories": {"query": "test", "limit": 1, "subcategory": "__invalid__"},
    }
    for key, params in probes.items():
        try:
            await call_find_jobs(session, "search", org_uid, params)
            logger.warning("%s probe was accepted; no list returned", key)
        except Exception as exc:
            found = _extract_list(str(exc))
            if found:
                options[key] = found
                logger.info("Discovered %d %s", len(found), key)
            else:
                logger.warning("Could not parse %s list from: %s", key, str(exc)[:300])

    try:
        payload = await call_find_jobs(
            session, "search", org_uid,
            {"query": "test", "limit": 1, "skills": ["__invalid_skill__"]},
        )
        note = payload.get("skills_note") or ""
        found = _extract_list(note)
        if found:
            options["skills"] = found
            logger.info("Discovered %d skills", len(found))
        else:
            logger.info("No skill list returned; skills stay free-text with validation")
    except Exception as exc:
        found = _extract_list(str(exc))
        if found:
            options["skills"] = found

    return options
