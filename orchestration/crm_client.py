from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import Any
import httpx2
from crm_payload import to_crm_payload

logger = logging.getLogger("crm_client")

REQUEST_TIMEOUT_SECONDS = 60.0
SEND_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5.0


def crm_endpoint(config: dict) -> str | None:
    return (os.getenv("UPWORK_CRM_ENDPOINT") or config.get("crm_endpoint") or "").strip() or None


def crm_enabled(config: dict) -> bool:
    return bool(config.get("crm_enabled", True)) and crm_endpoint(config) is not None


def build_request_body(config: dict, payload: list[dict[str, Any]]) -> Any:
    style = (config.get("crm_payload_style") or "jsonrpc").lower()
    if style == "raw":
        return payload
    key = config.get("crm_params_key") or "jobs"
    return {"jsonrpc": "2.0", "method": "call", "params": {key: payload}, "id": None}


def interpret_response(raw: str) -> tuple[bool, str]:
    """Odoo answers 200 even when it rejects the jobs, so read the JSON-RPC body."""
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return True, raw[:200]

    if isinstance(body.get("error"), dict):
        return False, str(body["error"].get("message") or body["error"])[:300]

    result = body.get("result")
    if not isinstance(result, dict):
        return True, str(result)[:200]

    if str(result.get("status", "")).lower() == "error":
        return False, str(result.get("message") or result)[:300]

    created = result.get("created_count")
    ignored = result.get("ignored_count")
    if created is not None or ignored is not None:
        return True, f"created {created}, ignored {ignored}"
    return True, str(result.get("message") or result)[:200]


async def send_jobs(config: dict, records: list[dict[str, Any]]) -> bool:
    if not records:
        return True

    url = crm_endpoint(config)
    if not url:
        logger.debug("No CRM endpoint configured; skipping push")
        return True

    payload = to_crm_payload(records)
    body = build_request_body(config, payload)
    headers = {"Content-Type": "application/json", "ngrok-skip-browser-warning": "1"}

    delay = RETRY_DELAY_SECONDS
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            async with httpx2.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=body, headers=headers)
            if 200 <= response.status_code < 300:
                raw = (await response.aread()).decode("utf-8", "replace")
                accepted, summary = interpret_response(raw)
                if accepted:
                    logger.info("Pushed %d job(s) to CRM: %s", len(payload), summary)
                    return True
                logger.warning(
                    "CRM accepted the request but rejected the jobs "
                    "(attempt %d/%d): %s",
                    attempt, SEND_ATTEMPTS, summary,
                )
            body = (await response.aread()).decode("utf-8", "replace")[:300]
            logger.warning(
                "CRM rejected the push (attempt %d/%d): HTTP %d %s",
                attempt, SEND_ATTEMPTS, response.status_code, body,
            )
        except Exception:
            logger.warning(
                "CRM push failed (attempt %d/%d)", attempt, SEND_ATTEMPTS, exc_info=True
            )

        if attempt < SEND_ATTEMPTS:
            await asyncio.sleep(delay)
            delay *= 2

    logger.error(
        "Giving up on CRM push of %d job(s). The timestamp is not advanced, so they "
        "are retried on the next poll.",
        len(payload),
    )
    return False
