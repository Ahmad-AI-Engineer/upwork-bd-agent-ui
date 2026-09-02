from __future__ import annotations

from typing import Any

JOB_INFORMATION_MAP = {
    "job_id": "search.id",
    "title": "search.title",
    "url": "search.url",
    "status": None,
    "posted_at": "search.published_date",
    "created_at": "search.created_date",
    "category": None,
    "category_group": None,
    "occupation": None,
    "experience_level": "search.experience_level",
    "contractor_tier": None,
    "is_contract_to_hire": None,
    "number_of_positions": "search.freelancers_to_hire",
}

CONTRACT_INFORMATION_MAP = {
    "job_type": "search.job_type",
    "budget_fixed": "search.budget",
    "budget_hourly_min": "get.data.marketplaceJobPosting.contractTerms.hourlyContractTerms.hourlyBudgetMin",
    "budget_hourly_max": "get.data.marketplaceJobPosting.contractTerms.hourlyContractTerms.hourlyBudgetMax",
    "budget_duration": "search.duration",
    "workload": "search.engagement",
}

JOB_REQUIREMENTS_MAP = {
    "skills": "search.skills",
    "description": "get.data.marketplaceJobPosting.content.description",
    "countries_required": "get.preferred_locations.countries",
    "min_hours_per_week": None,
    "local_market_only": "get.preferred_locations.location_required",
}

CLIENT_INFORMATION_MAP = {
    "client_country": "search.client.country",
    "client_city": None,
    "client_timezone": "get.data.marketplaceJobPosting.clientCompanyPublic.timezone",
    "client_industry": None,
    "client_company_size": None,
    "client_member_since": None,
    "client_payment_verified": "search.client.verification_status",
    "client_hire_rate": None,
    "client_avg_hourly_rate": None,
    "client_jobs_posted": "search.client.total_posted_jobs",
    "client_jobs_open": None,
    "client_total_hires": "search.client.total_hires",
    "client_active_hires": "get.client_record.contracts_active",
    "client_total_hours": "get.client_record.hours_total",
    "client_total_spent": "search.client.total_spent",
    "client_feedback_score": "search.client.rating",
    "client_feedback_count": "search.client.total_reviews",
}

APPLICATION_INFORMATION_MAP = {
    "total_applicants": "search.proposal_count",
    "total_hired": "get.data.marketplaceJobPosting.activityStat.jobActivity.totalHired",
    "invitations_sent": "get.data.marketplaceJobPosting.activityStat.jobActivity.invitesSent",
    "invited_to_interview": "get.data.marketplaceJobPosting.activityStat.jobActivity.totalInvitedToInterview",
    "last_client_activity": None,
}

REQUIRED_FIELD_SECTIONS = {
    "job_information": JOB_INFORMATION_MAP,
    "contract_information": CONTRACT_INFORMATION_MAP,
    "job_requirements": JOB_REQUIREMENTS_MAP,
    "client_information": CLIENT_INFORMATION_MAP,
    "application_information": APPLICATION_INFORMATION_MAP,
}

_MAPPED_SOURCE_KEYS = {
    source_key
    for section in REQUIRED_FIELD_SECTIONS.values()
    for source_key in section.values()
    if source_key is not None
}


def _extract_required_fields(flat_row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        section_name: {
            field_name: flat_row.get(source_key) if source_key is not None else None
            for field_name, source_key in field_map.items()
        }
        for section_name, field_map in REQUIRED_FIELD_SECTIONS.items()
    }


def _extract_additional_fields(flat_row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in flat_row.items()
        if key not in _MAPPED_SOURCE_KEYS and key != "_domain"
    }


def to_report_record(flat_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "required_fields": _extract_required_fields(flat_row),
        "additional_mcp_fields": _extract_additional_fields(flat_row),
    }
