from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

import env_bootstrap  # noqa: F401
from crm_client import crm_enabled, send_jobs
from crm_payload import to_crm_payload
from job_flatten import flatten_job_record
from search_filters import build_search_params
from job_schema import to_report_record
from rate_limiter import RateLimiter
from upwork_connection_handler import (
    AuthorizationRequiredError,
    PermanentUpworkError,
    ensure_access_token,
    run_with_session,
)

if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger("job_poller")

_BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_PATH = Path(os.getenv("UPWORK_CONFIG_PATH", _BASE_DIR / "config" / "settings.json"))
DEFAULT_POLL_INTERVAL_MINUTES = 15
MIN_POLL_INTERVAL_MINUTES = 15
JOBS_PER_DOMAIN = 10
FIND_JOBS_TOOL = "upwork__find_jobs"
LIST_ACCOUNTS_TOOL = "upwork__list_accounts"
RECONNECT_BACKOFF_SECONDS = 30
INTERVAL_RECHECK_SECONDS = 5
FIND_JOBS_PER_MINUTE = 11
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_BACKOFF_SECONDS = 8.0

find_jobs_limiter = RateLimiter(FIND_JOBS_PER_MINUTE)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def save_cursor(domain_name: str, cursor: str) -> None:
    config = load_config()
    for domain in config.get("domains", []):
        if domain_label(domain) != domain_name:
            continue
        if domain.get("last_seen_posted_at") == cursor:
            return
        domain["last_seen_posted_at"] = cursor
        save_config(config)
        logger.debug("[%s] timestamp saved: %s", domain_name, cursor)
        return


TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
)


def parse_timestamp(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "throttl",
    "quota",
)


def is_rate_limited(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def response_text(result) -> str:
    """First text block of a tool result, or "" when there is nothing to read."""
    content = getattr(result, "content", None) or []
    if not content:
        return ""
    return str(getattr(content[0], "text", "") or "")


def parse_tool_result(label: str, result) -> tuple[dict | None, str | None]:
    """Return (payload, reason). Exactly one is set; a reason means the call failed.

    Upwork does not always answer with the documented {"status": "error", "reason": ...}
    JSON. Errors also arrive as an isError result or as bare text ("Too Many Requests",
    "The org_uid provided is not one of your accounts"), which used to crash this on
    json.loads and hide what Upwork actually said.
    """
    text = response_text(result)
    failed = bool(getattr(result, "isError", False))

    if not text.strip():
        return None, "empty response from Upwork"

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(
            "%s returned non-JSON (%d chars): %s",
            label, len(text), text[:300],
        )
        return None, text.strip()[:300]

    if not isinstance(payload, dict):
        return None, f"unexpected response type {type(payload).__name__}"

    if failed or payload.get("status") == "error":
        return None, str(payload.get("reason", payload))[:300]

    return payload, None


class AccountMismatchError(PermanentUpworkError):
    """The configured org_uid is not one the authorized token can act for."""


def describe_accounts(accounts: list[dict]) -> str:
    return ", ".join(
        f"{a.get('name')} ({a.get('role_label')}) = {a.get('org_uid')}" for a in accounts
    ) or "none"


async def verify_account(session, org_uid: str) -> None:
    """Fail the cycle early if the token cannot act for the configured org_uid.

    One call, once per cycle. Without it a mismatched token fails identically on every
    query instead, which reads as nine unrelated errors rather than one auth problem.
    """
    result = await session.call_tool(LIST_ACCOUNTS_TOOL, {})
    payload, reason = parse_tool_result("list_accounts", result)

    if reason is not None:
        raise AccountMismatchError(f"Could not verify the Upwork account: {reason}")

    accounts = payload.get("accounts") or []
    match = next((a for a in accounts if str(a.get("org_uid")) == str(org_uid)), None)

    if match is None:
        raise AccountMismatchError(
            f"Configured org_uid {org_uid} is not available to the authorized token, "
            f"which can act for: {describe_accounts(accounts)}. "
            "Re-run 'python orchestration/upwork_connection_handler.py' while signed in "
            "to Upwork as the right user, or fix org_uid in the config."
        )

    logger.info(
        "Polling as %s (%s)", match.get("name"), match.get("role_label") or "unknown role"
    )


async def call_find_jobs(session, action: str, org_uid: str, params: dict) -> dict:
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        await find_jobs_limiter.acquire()
        result = await session.call_tool(
            FIND_JOBS_TOOL,
            {"action": action, "org_uid": org_uid, "params": params},
        )
        payload, reason = parse_tool_result(f"find_jobs {action}", result)

        if reason is None:
            return payload
        if not is_rate_limited(reason) or attempt == RATE_LIMIT_ATTEMPTS:
            raise RuntimeError(f"find_jobs {action} error: {reason}")

        delay = RATE_LIMIT_BACKOFF_SECONDS * attempt
        find_jobs_limiter.pause(delay)
        logger.warning(
            "Rate limited by Upwork on find_jobs %s; waiting %.0fs (attempt %d/%d)",
            action, delay, attempt, RATE_LIMIT_ATTEMPTS,
        )
        await asyncio.sleep(delay)

    raise RuntimeError(f"find_jobs {action} error: rate limit not cleared")


def domain_label(domain: dict) -> str:
    filters = domain.get("filters") or {}
    return (filters.get("query") or domain.get("query") or domain.get("name") or "").strip()


def domain_filters(domain: dict) -> dict:
    filters = dict(domain.get("filters") or {})
    if "query" not in filters and domain.get("query"):
        filters["query"] = domain["query"]
    filters.setdefault("limit", JOBS_PER_DOMAIN)
    return filters


async def fetch_latest_jobs(session, org_uid: str, filters: dict) -> list[dict]:
    params = build_search_params(filters)
    logger.debug("find_jobs search params: %s", params)
    payload = await call_find_jobs(session, "search", org_uid, params)
    if payload.get("skills_note"):
        logger.warning("Upwork skills note: %s", payload["skills_note"])
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


def records_from_dataframe(df: pl.DataFrame) -> list[dict]:
    return [to_report_record(_unflatten_json_strings(row)) for row in df.to_dicts()]


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


def log_flag_summary(total_jobs: int, flagged_jobs: int, rule_counts: dict[str, int]) -> None:
    logger.info(
        "Job flag summary: total_jobs=%d, flagged_jobs=%d, rule_counts=%s",
        total_jobs, flagged_jobs, json.dumps(rule_counts),
    )


async def poll_once(session, config: dict) -> None:
    org_uid = config["org_uid"]
    total_jobs = 0
    flagged_jobs = 0
    rule_counts = {f"R{i}": 0 for i in range(1, 22)}
    logger.info("Poll run starting")

    for domain in config["domains"]:
        domain_name = domain_label(domain)
        if not domain.get("enabled", True):
            logger.info("[%s] disabled, skipping", domain_name)
            continue

        try:
            jobs = await fetch_latest_jobs(session, org_uid, domain_filters(domain))
            new_jobs = filter_new_jobs(jobs, domain.get("last_seen_posted_at"))
            pushed = True

            if not new_jobs:
                logger.info("[%s] no new jobs", domain_name)
            else:
                logger.info("[%s] %d new job(s), fetching full detail", domain_name, len(new_jobs))
                df = await build_jobs_dataframe(session, org_uid, domain_name, new_jobs)
                log_jobs_preview(df)
                records = records_from_dataframe(df)
                for record in records:
                    record.update({
                        "query_id": domain.get("query_id"),
                        "query_name": domain.get("name"),
                        "practice_id": domain.get("practice_id"),
                        "practice_name": domain.get("practice_name"),
                        "last_run": domain.get("last_run"),
                    })

                for payload in to_crm_payload(records):
                    total_jobs += 1
                    flagged_jobs += int(payload["flagged"])
                    for rule in payload["flag_reasons"]:
                        rule_counts[rule] += 1

                if not crm_enabled(config):
                    logger.warning(
                        "[%s] CRM push disabled; %d job(s) not sent anywhere",
                        domain_name, len(records),
                    )
                else:
                    pushed = await send_jobs(config, records)
                    if not pushed:
                        logger.error(
                            "[%s] push failed; leaving the timestamp so these %d job(s) "
                            "retry next cycle", domain_name, len(records),
                        )

            if jobs and pushed:
                newest = max(jobs, key=lambda j: parse_timestamp(j["created_date"]))
                save_cursor(domain_name, newest["created_date"])
        except PermanentUpworkError:
            log_flag_summary(total_jobs, flagged_jobs, rule_counts)
            raise
        except Exception:
            # The cursor stays put, so this query's jobs are retried next cycle
            # instead of the whole run dying on one bad response.
            logger.exception("[%s] failed; skipping this query for this cycle", domain_name)

    log_flag_summary(total_jobs, flagged_jobs, rule_counts)


def poll_interval_seconds(config: dict) -> float:
    minutes = config.get("poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES)
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        minutes = DEFAULT_POLL_INTERVAL_MINUTES
    return max(float(MIN_POLL_INTERVAL_MINUTES), minutes) * 60


def warn_if_interval_too_short(config: dict, domains: list[dict]) -> None:
    calls = len(domains) + sum(
        int(domain_filters(d).get("limit") or JOBS_PER_DOMAIN) for d in domains
    )
    needed = calls * find_jobs_limiter.min_interval
    interval = poll_interval_seconds(config)
    if needed > interval:
        logger.warning(
            "A full cycle needs about %.0fs at %d requests/min (%d calls), but the poll "
            "interval is %.0fs. Cycles will overlap: raise the interval or lower the "
            "per-query limit.",
            needed, FIND_JOBS_PER_MINUTE, calls, interval,
        )


async def run_once() -> None:
    await ensure_access_token()

    config = load_config()
    domains = [d for d in config.get("domains", []) if d.get("enabled", True)]
    logger.info(
        "Settings loaded: %d search quer%s (%s)",
        len(domains),
        "y" if len(domains) == 1 else "ies",
        ", ".join(domain_label(d) or "unnamed" for d in domains) or "none",
    )
    warn_if_interval_too_short(config, domains)

    async def _poll(session) -> None:
        logger.info("Connected to Upwork MCP server")
        await verify_account(session, config["org_uid"])
        await poll_once(session, config)

    await run_with_session(_poll)
    logger.info("Poll run complete")


async def wait_for_next_poll() -> None:
    started = time.monotonic()
    announced: float | None = None

    while True:
        interval = poll_interval_seconds(load_config())

        if announced is None:
            logger.info("Next poll in %.0f minute(s)", interval / 60)
        elif interval != announced:
            logger.info("Poll interval changed to %.0f minute(s)", interval / 60)
        announced = interval

        remaining = interval - (time.monotonic() - started)
        if remaining <= 0:
            return
        await asyncio.sleep(min(INTERVAL_RECHECK_SECONDS, remaining))


async def run_forever() -> None:
    while True:
        try:
            await run_once()
        except PermanentUpworkError as exc:
            logger.error("%s", exc)
        except Exception:
            logger.exception("Poll cycle failed")

        await wait_for_next_poll()


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
    except PermanentUpworkError as exc:
        # Reported here rather than re-raised: a traceback would only bury the message.
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
