from __future__ import annotations

import json
from typing import Any


def _flatten(obj: Any, prefix: str = "", out: dict | None = None) -> dict:
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            _flatten(value, path, out)
    elif isinstance(obj, list) and any(isinstance(item, dict) for item in obj):
        out[prefix] = json.dumps(obj)
    else:
        out[prefix] = obj
    return out


def flatten_search_result(job: dict) -> dict:
    return _flatten(job, prefix="search")


def flatten_get_result(job_detail: dict) -> dict:
    return _flatten(job_detail, prefix="get")


def flatten_job_record(search_job: dict, get_payload: dict) -> dict:
    row: dict = {}
    row.update(flatten_search_result(search_job))
    row.update(flatten_get_result(get_payload))
    return row
