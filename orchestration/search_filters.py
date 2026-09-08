from __future__ import annotations

from typing import Any

TEXT = "text"
NUMBER = "number"
SELECT = "select"
BOOL = "bool"
TAGS = "tags"
MULTISELECT = "multiselect"

MONEY_STEPS = [0, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
RATE_STEPS = [0, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150]
COUNT_STEPS = [0, 1, 2, 5, 10, 15, 20, 30, 50]
HIRE_STEPS = [0, 1, 5, 10, 25, 50, 100]

FILTER_SCHEMA: list[dict[str, Any]] = [
    {
        "group": "Search",
        "fields": [
            {"name": "query", "type": TEXT, "label": "Search query",
             "help": "Free-text marketplace search, e.g. 'Power BI'."},
            {"name": "sort", "type": SELECT, "label": "Sort by", "default": "recency",
             "options": ["recency", "relevance", "client_total_charge", "client_rating"],
             "help": "Leave on recency for a poller; newest first."},
            {"name": "limit", "type": SELECT, "label": "Results per run", "default": 10,
             "options": [1, 2, 3, 5, 10], "numeric": True,
             "help": "Upwork caps this at 10 per request."},
            {"name": "category", "type": SELECT, "label": "Category", "options_from": "categories",
             "help": "Top-level category name, e.g. 'Web, Mobile & Software Dev'. Unknown names are rejected."},
            {"name": "subcategory", "type": SELECT, "label": "Subcategory", "options_from": "subcategories",
             "help": "One level below category."},
            {"name": "skills", "type": MULTISELECT, "label": "Skills (max 5)", "max_items": 5, "options_from": "skills",
             "help": "Must match Upwork skill names exactly or they are silently dropped."},
        ],
    },
    {
        "group": "Contract",
        "fields": [
            {"name": "job_type", "type": SELECT, "label": "Job type",
             "options": ["fixed", "hourly"],
             "help": "Budget filters apply to fixed; rate filters apply to hourly."},
            {"name": "experience_level", "type": SELECT, "label": "Experience level",
             "options": ["entry_level", "intermediate", "expert"]},
            {"name": "budget_min", "type": SELECT, "label": "Budget min ($)", "options": MONEY_STEPS, "numeric": True,
             "help": "FIXED-price only. Does not filter hourly jobs."},
            {"name": "budget_max", "type": SELECT, "label": "Budget max ($)", "options": MONEY_STEPS, "numeric": True},
            {"name": "rate_min", "type": SELECT, "label": "Hourly rate min ($/hr)", "options": RATE_STEPS, "numeric": True,
             "help": "HOURLY only. Pair with job_type=hourly."},
            {"name": "rate_max", "type": SELECT, "label": "Hourly rate max ($/hr)", "options": RATE_STEPS, "numeric": True},
            {"name": "workload", "type": SELECT, "label": "Workload",
             "options": ["full_time", "part_time", "as_needed"],
             "help": "Hours per week."},
            {"name": "duration", "type": SELECT, "label": "Project length",
             "options": ["less_than_1_month", "1_to_3_months", "3_to_6_months", "more_than_6_months"],
             "help": "Constrains HOURLY jobs only; pair with job_type=hourly."},
        ],
    },
    {
        "group": "Competition",
        "fields": [
            {"name": "proposals_min", "type": SELECT, "label": "Proposals min", "options": COUNT_STEPS, "numeric": True},
            {"name": "proposals_max", "type": SELECT, "label": "Proposals max", "options": COUNT_STEPS, "numeric": True,
             "help": "e.g. 5 to only see low-competition jobs."},
        ],
    },
    {
        "group": "Client",
        "fields": [
            {"name": "verified_payment_only", "type": BOOL, "label": "Verified payment only"},
            {"name": "previous_clients_only", "type": BOOL, "label": "Previous clients only"},
            {"name": "client_hires_min", "type": SELECT, "label": "Client hires min", "options": HIRE_STEPS, "numeric": True,
             "help": "Set both min and max to 0 for clients who never hired."},
            {"name": "client_hires_max", "type": SELECT, "label": "Client hires max", "options": HIRE_STEPS, "numeric": True},
            {"name": "timezone", "type": SELECT, "label": "Client timezone", "options_from": "timezones"},
            {"name": "location", "type": SELECT, "label": "Client location", "options_from": "locations"},
        ],
    },
]

ALL_FIELDS = {f["name"]: f for g in FILTER_SCHEMA for f in g["fields"]}


def build_search_params(filters: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name, value in (filters or {}).items():
        if name not in ALL_FIELDS:
            continue
        if value is None or value == "" or value == []:
            continue
        params[name] = value
    params.setdefault("query", "")
    params.setdefault("limit", 10)
    params.setdefault("sort", "recency")
    return params


def validate_filters(filters: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    f = filters or {}

    limit = f.get("limit")
    if limit not in (None, "") and not (1 <= int(limit) <= 10):
        errors.append("limit must be between 1 and 10")

    skills = f.get("skills") or []
    if len(skills) > 5:
        errors.append("skills accepts at most 5 entries")

    for lo, hi, label in (
        ("budget_min", "budget_max", "budget"),
        ("rate_min", "rate_max", "rate"),
        ("proposals_min", "proposals_max", "proposals"),
        ("client_hires_min", "client_hires_max", "client hires"),
    ):
        lo_v, hi_v = f.get(lo), f.get(hi)
        if lo_v not in (None, "") and hi_v not in (None, "") and float(lo_v) > float(hi_v):
            errors.append(f"{label} min cannot exceed max")

    if not (f.get("query") or "").strip() and not (f.get("skills") or []):
        errors.append("provide a search query or at least one skill")

    return errors
