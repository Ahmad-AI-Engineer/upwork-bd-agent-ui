from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from job_flatten import flatten_job_record
from job_schema import to_report_record
from upwork_connection import run_with_session, upwork_session

if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger("job_poller")

_BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = Path(os.getenv("UPWORK_CONFIG_PATH", _BASE_DIR / "config" / "domains.json"))
OUTPUT_DIR = Path(os.getenv("UPWORK_JOBS_DIR", _BASE_DIR / "jobs"))
LOG_DIR = Path(os.getenv("UPWORK_LOGS_DIR", _BASE_DIR / "logs"))
POLL_INTERVAL_SECONDS = 60
JOBS_PER_DOMAIN = 10
FIND_JOBS_TOOL = "upwork__find_jobs"
RECONNECT_BACKOFF_SECONDS = 30


def configure_logging(level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"job_poller_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(stream_handler)
    root.addHandler(file_handler)

    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


async def call_find_jobs(session, action: str, org_uid: str, params: dict) -> dict:
    result = await session.call_tool(
        FIND_JOBS_TOOL,
        {"action": action, "org_uid": org_uid, "params": params},
    )
    payload = json.loads(result.content[0].text)
    if payload.get("status") == "error":
        raise RuntimeError(f"find_jobs {action} error: {payload.get('reason', payload)}")
    return payload


async def fetch_latest_jobs(session, org_uid: str, query: str, limit: int) -> list[dict]:
    payload = await call_find_jobs(
        session, "search", org_uid, {"query": query, "limit": limit, "sort": "recency"}
    )
    return payload.get("jobs", [])


async def fetch_job_detail(session, org_uid: str, job_id: str) -> dict:
    return await call_find_jobs(session, "get", org_uid, {"id": job_id})


def filter_new_jobs(jobs: list[dict], last_seen_posted_at: str | None) -> list[dict]:
    if not last_seen_posted_at:
        return jobs
    cutoff = parse_timestamp(last_seen_posted_at)
    return [j for j in jobs if parse_timestamp(j["created_date"]) > cutoff]


async def build_jobs_dataframe(session, org_uid: str, domain_name: str, new_jobs: list[dict]) -> pl.DataFrame:
    rows = []
    for job in new_jobs:
        detail = await fetch_job_detail(session, org_uid, job["id"])
        logger.debug("Fetched detail for job %s (%s)", job["id"], job.get("title"))
        row = flatten_job_record(job, detail)
        row["_domain"] = domain_name
        rows.append(row)
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None, strict=False)


def _looks_like_json(value: Any) -> bool:
    return isinstance(value, str) and value[:1] in "[{"


def _unflatten_json_strings(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if _looks_like_json(value):
            try:
                out[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        out[key] = value
    return out


def report_path_for_today() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"upwork_jobs_{stamp}.json"


def load_report(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"generated_at": None, "source": "Upwork MCP", "domains": {}}


def append_jobs_to_report(df: pl.DataFrame, domain_name: str) -> Path | None:
    if df.is_empty():
        return None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = report_path_for_today()
    report = load_report(out_path)
    domain_jobs = report["domains"].setdefault(domain_name, [])
    existing_ids = {
        job["required_fields"]["job_information"]["job_id"] for job in domain_jobs
    }

    added = 0
    for row in df.to_dicts():
        flat_row = _unflatten_json_strings(row)
        record = to_report_record(flat_row)
        job_id = record["required_fields"]["job_information"]["job_id"]
        if job_id not in existing_ids:
            domain_jobs.append(record)
            existing_ids.add(job_id)
            added += 1
        else:
            logger.debug("Skipping duplicate job_id %s", job_id)

    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    out_path.write_text(json.dumps(report, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    logger.info("Appended %d new job(s) to %s", added, out_path)
    return out_path


def log_jobs_preview(df: pl.DataFrame) -> None:
    try:
        columns = df.select(
            "search.title",
            "search.created_date",
            "search.budget",
            "get.connects_cost",
            "search.url",
        )
        with pl.Config(tbl_cols=-1, tbl_width_chars=220, fmt_str_lengths=80, tbl_rows=-1):
            rendered = str(columns)
        logger.info("Jobs preview:\n%s", rendered)
    except Exception:
        logger.exception("Failed to render jobs preview; data was already saved")


async def poll_once(session, config: dict) -> None:
    org_uid = config["org_uid"]
    logger.info("Poll run starting")

    for domain in config["domains"]:
        domain_name = domain["name"]
        jobs = await fetch_latest_jobs(session, org_uid, domain["query"], JOBS_PER_DOMAIN)
        new_jobs = filter_new_jobs(jobs, domain.get("last_seen_posted_at"))

        if not new_jobs:
            logger.info("[%s] no new jobs", domain_name)
        else:
            logger.info("[%s] %d new job(s), fetching full detail", domain_name, len(new_jobs))
            df = await build_jobs_dataframe(session, org_uid, domain_name, new_jobs)
            append_jobs_to_report(df, domain_name)
            log_jobs_preview(df)

        if jobs:
            newest = max(jobs, key=lambda j: parse_timestamp(j["created_date"]))
            domain["last_seen_posted_at"] = newest["created_date"]
            logger.debug("[%s] cursor advanced to %s", domain_name, newest["created_date"])

    save_config(config)


async def run_once() -> None:
    config = load_config()

    async def _poll(session) -> None:
        logger.info("Connected to Upwork MCP server")
        await poll_once(session, config)

    await run_with_session(_poll)
    logger.info("Poll run complete")


async def run_forever() -> None:
    config = load_config()
    while True:
        try:
            async with upwork_session() as session:
                logger.info("Connected to Upwork MCP server")
                while True:
                    try:
                        await poll_once(session, config)
                    except Exception:
                        logger.exception("Poll failed")
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception:
            logger.exception("Connection error; reconnecting in %ds", RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll Upwork for new jobs per configured domain")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously with an internal timer instead of exiting after one poll",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging(getattr(logging, args.log_level.upper(), logging.INFO))

    try:
        asyncio.run(run_forever() if args.daemon else run_once())
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
