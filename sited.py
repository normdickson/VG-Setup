"""
sited.py — SiteDocs API calls for location creation.

SiteDocs Name convention matches the observed pattern in create_location.py:
    Name = "{job_number} ({company_name})"
    Description = "{company_name}"
    Address = "{location}"

API key MUST be supplied via SITEDOCS_API_KEY env var.
There is no hardcoded fallback to prevent accidental key exposure.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

import requests

log = logging.getLogger("sited")

# ---------------------------------------------------------------------------
# Config — no hardcoded secrets
# ---------------------------------------------------------------------------

def _require_api_key() -> str:
    key = os.environ.get("SITEDOCS_API_KEY", "")
    if not key:
        raise RuntimeError(
            "SITEDOCS_API_KEY environment variable is not set. "
            "Cannot create SiteDocs location records."
        )
    return key


SITEDOCS_COMPANY_ID = os.environ.get(
    "SITEDOCS_COMPANY_ID",
    "48651caf-50e4-45ab-875a-dfe02fa14441",  # fixed per requirements
)

SITEDOCS_URL = "https://api-1.sitedocs.com/api/v1/locations"

# Field length limits observed from the reference create_location.py
_NAME_MAX        = 100
_DESCRIPTION_MAX = 500
_ADDRESS_MAX     = 255


def _trunc(value: Optional[str], max_len: int) -> str:
    """Return value truncated to max_len, or empty string if None/empty."""
    if not value:
        return ""
    return value[:max_len]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_location(
    job_number:     str,
    job_description: Optional[str],
    location:       Optional[str],
    job_date_iso:   Optional[str],
    company_name:   Optional[str] = None,
) -> str:
    """
    Create a Location record in SiteDocs.

    Args:
        job_number:      Job identifier (required).
        job_description: Job description text (used only as fallback description).
        location:        Address / locality string.
        job_date_iso:    ISO-8601 date string for StartDate (e.g. "2026-02-21T00:00:00").
        company_name:    Client company name.  When provided, the SiteDocs Name
                         is formatted as "{job_number} ({company_name})", matching
                         the convention in create_location.py.

    Returns:
        Location ID string from SiteDocs response.

    Raises:
        RuntimeError if SITEDOCS_API_KEY is not set or on API error.
        ValueError  if job_number is empty.
    """
    if not job_number or not job_number.strip():
        raise ValueError("job_number must be a non-empty string")

    api_key = _require_api_key()

    # Build the Name field
    if company_name and company_name.strip():
        name = f"{job_number} ({company_name.strip()})"
    else:
        name = job_number

    # Build the Description field
    description = _trunc(company_name or job_description, _DESCRIPTION_MAX)

    payload = {
        "Name":              _trunc(name, _NAME_MAX),
        "Description":       description,
        "Address":           _trunc(location, _ADDRESS_MAX),
        "StartDate":         job_date_iso or "2026-01-01T00:00:00",
        "CreatingCompanyId": SITEDOCS_COMPANY_ID,
        "IsArchived":        False,   # Boolean, not the string "false"
    }

    headers = {
        "Accept":        "application/json",
        "Authorization": api_key,
        "Content-Type":  "application/json",
    }

    log.info("Creating SiteDocs location for job %s", job_number)

    try:
        response = requests.post(
            SITEDOCS_URL, headers=headers, json=payload, timeout=30
        )
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(
            f"SiteDocs API timeout for job {job_number}"
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code
        body   = exc.response.text[:500]
        raise RuntimeError(
            f"SiteDocs API returned HTTP {status} for job {job_number}: {body}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"SiteDocs request failed for job {job_number}: {exc}"
        ) from exc

    data = response.json()
    # SiteDocs may return "id" or "Id" depending on API version
    location_id = data.get("id") or data.get("Id")

    if not location_id:
        raise RuntimeError(
            f"SiteDocs response for job {job_number} did not include a location ID. "
            f"Response body: {str(data)[:300]}"
        )

    log.info("SiteDocs location created for job %s: id=%s", job_number, location_id)
    return str(location_id)
