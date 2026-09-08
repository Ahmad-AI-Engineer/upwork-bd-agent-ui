from __future__ import annotations

import re
from datetime import datetime
from typing import Any

TIMEZONE_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Chongqing": "Asia/Shanghai",
    "Asia/Chungking": "Asia/Shanghai",
    "Asia/Harbin": "Asia/Shanghai",
    "Asia/Macao": "Asia/Macau",
    "Asia/Dacca": "Asia/Dhaka",
    "Asia/Istanbul": "Europe/Istanbul",
    "Europe/Kiev": "Europe/Kyiv",
    "Europe/Nicosia": "Asia/Nicosia",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Godthab": "America/Nuuk",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Pacific/Ponape": "Pacific/Pohnpei",
    "Pacific/Truk": "Pacific/Chuuk",
    "Australia/Canberra": "Australia/Sydney",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}

EXPERIENCE_LEVEL_LABELS = {
    "ENTRY_LEVEL": "EntryLevel",
    "INTERMEDIATE": "IntermediateLevel",
    "EXPERT": "ExpertLevel",
}

MCP = "additional_mcp_fields"
CLIENT_ID_KEY = "get.data.marketplaceJobPosting.clientCompanyPublic.id"
SCREENING_KEY = "get.screening_questions"


def _to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_iso_z(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.astimezone(tz=None).strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"
    return text


def _join(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _strip_untrusted(value: Any) -> str | None:
    text = _join(value)
    if not text:
        return None
    text = re.sub(r"</?untrusted_participant_content>", "", text)
    return text.strip() or None


def _canonical_timezone(value: Any) -> str | None:
    name = _join(value)
    if not name:
        return None
    return TIMEZONE_ALIASES.get(name, name)


def to_crm_job(record: dict[str, Any]) -> dict[str, Any]:
    required = record.get("required_fields", {})
    job = required.get("job_information", {})
    contract = required.get("contract_information", {})
    reqs = required.get("job_requirements", {})
    client = required.get("client_information", {})
    application = required.get("application_information", {})
    extra = record.get(MCP, {})

    job_type = (contract.get("job_type") or "").upper() or None
    level = (job.get("experience_level") or "").upper()

    budget_fixed = _to_number(contract.get("budget_fixed")) if job_type == "FIXED" else None
    if budget_fixed == 0:
        budget_fixed = None

    verified = client.get("client_payment_verified")
    payment_verified = (
        verified if isinstance(verified, bool) else str(verified).upper() == "VERIFIED"
    )

    return {
        "job_id": job.get("job_id"),
        "title": job.get("title"),
        "url": job.get("url"),
        "status": job.get("status"),
        "posted_at": _to_iso_z(job.get("posted_at")),
        "created_at": _to_iso_z(job.get("created_at")),
        "category": job.get("category"),
        "category_group": job.get("category_group"),
        "occupation": job.get("occupation"),
        "experience_level": EXPERIENCE_LEVEL_LABELS.get(level),
        "contractor_tier": level or None,
        "is_contract_to_hire": job.get("is_contract_to_hire"),
        "number_of_positions": job.get("number_of_positions"),
        "job_type": job_type,
        "budget_fixed": budget_fixed,
        "budget_hourly_min": _to_number(contract.get("budget_hourly_min")),
        "budget_hourly_max": _to_number(contract.get("budget_hourly_max")),
        "budget_duration": contract.get("budget_duration"),
        "workload": contract.get("workload"),
        "skills": _join(reqs.get("skills")),
        "description": _strip_untrusted(reqs.get("description")),
        "screening_questions": _strip_untrusted(extra.get(SCREENING_KEY)),
        "client_name": extra.get(CLIENT_ID_KEY),
        "client_country": client.get("client_country"),
        "client_city": client.get("client_city"),
        "client_timezone": _canonical_timezone(client.get("client_timezone")),
        "client_industry": client.get("client_industry"),
        "client_company_size": client.get("client_company_size"),
        "client_company_id": extra.get(CLIENT_ID_KEY),
        "client_member_since": _to_iso_z(client.get("client_member_since")),
        "client_payment_verified": payment_verified,
        "client_hire_rate": _to_number(client.get("client_hire_rate")),
        "client_avg_hourly_rate": _to_number(client.get("client_avg_hourly_rate")),
        "client_jobs_posted": client.get("client_jobs_posted"),
        "client_jobs_open": client.get("client_jobs_open"),
        "client_total_hires": client.get("client_total_hires"),
        "client_active_hires": client.get("client_active_hires"),
        "client_total_hours": client.get("client_total_hours"),
        "client_total_spent": _to_number(client.get("client_total_spent")),
        "client_feedback_score": _to_number(client.get("client_feedback_score")),
        "client_feedback_count": client.get("client_feedback_count"),
        "total_applicants": application.get("total_applicants"),
        "total_hired": application.get("total_hired"),
        "invitations_sent": application.get("invitations_sent"),
        "invited_to_interview": application.get("invited_to_interview"),
        "last_client_activity": _to_iso_z(application.get("last_client_activity")),
        "countries_required": _join(reqs.get("countries_required")),
        "min_hours_per_week": reqs.get("min_hours_per_week"),
        "local_market_only": (
            bool(reqs.get("local_market_only"))
            if reqs.get("local_market_only") is not None
            else None
        ),
        "open_jobs": [],
    }


def to_crm_payload(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [to_crm_job(r) for r in records]
