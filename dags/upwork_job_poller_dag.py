from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pendulum
from airflow.sdk import dag, task

logger = logging.getLogger("upwork_dag")

DEFAULT_ARGS = {
    "owner": "axiom",
    "retries": 3,
    "retry_delay": timedelta(seconds=30),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="upwork_job_poller",
    description="Poll Upwork for new jobs per configured domain and write a daily JSON report",
    schedule="*/10 * * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["upwork", "ingestion"],
)
def upwork_job_poller():
    @task
    def poll_jobs() -> dict:
        from job_poller import load_config, poll_once
        from upwork_connection import run_with_session

        config = load_config()

        async def _poll(session) -> None:
            await poll_once(session, config)

        asyncio.run(run_with_session(_poll))
        return {"polled_at": datetime.now(timezone.utc).isoformat()}

    @task
    def summarize_report() -> dict:
        import json

        from job_poller import report_path_for_today

        path = report_path_for_today()
        if not path.exists():
            logger.info("No report file produced for today yet")
            return {"jobs_total": 0, "report": None}

        report = json.loads(path.read_text(encoding="utf-8"))
        counts = {domain: len(jobs) for domain, jobs in report.get("domains", {}).items()}
        total = sum(counts.values())
        logger.info("Report %s holds %d job(s): %s", path.name, total, counts)
        return {"jobs_total": total, "per_domain": counts, "report": str(path)}

    poll_jobs() >> summarize_report()


upwork_job_poller()
