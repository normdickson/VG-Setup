"""
latitude.py — Queries Latitude job data via the Azure Function API.

Replaces the previous pyodbc/SQL Server direct connection with HTTP calls
to the vg-lat-view-api Azure Function, which handles the VNet-connected
SQL Express connection internally.

Environment variables:
    LATITUDE_API_URL  — Base URL of the Azure Function App
                        e.g. https://vg-lat-view-api-d7cycyb8fbamf8bg.westus-01.azurewebsites.net
"""

from __future__ import annotations

import os
import logging
from typing import Optional, List

import requests

log = logging.getLogger("latitude")

_API_URL: Optional[str] = os.getenv("LATITUDE_API_URL", "").rstrip("/")


def _require_api_url() -> str:
    if not _API_URL:
        raise RuntimeError(
            "LATITUDE_API_URL environment variable is not set. "
            "Cannot connect to Latitude API."
        )
    return _API_URL


# ---------------------------------------------------------------------------
# LatitudeDB — HTTP wrapper matching the original pyodbc interface
# ---------------------------------------------------------------------------

class LatitudeDB:
    """
    Mirrors the original LatitudeDB interface but fetches data from the
    Azure Function API instead of querying SQL Server directly.
    """

    def __init__(self, api_url: str) -> None:
        self._api_url = api_url.rstrip("/")
        self._session = requests.Session()

    def _ensure_connection(self) -> None:
        """No-op — HTTP is stateless. Kept for interface compatibility."""
        pass

    def close(self) -> None:
        """Close the requests session."""
        self._session.close()

    def _get(self, path: str, params: dict = None) -> list:
        """Make a GET request to the Azure Function API."""
        url = f"{self._api_url}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(f"Cannot reach Latitude API at {url}: {exc}") from exc
        except requests.exceptions.Timeout:
            raise RuntimeError(f"Latitude API timed out: {url}")
        except requests.exceptions.HTTPError as exc:
            raise RuntimeError(f"Latitude API error {exc.response.status_code}: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Latitude API unexpected error: {exc}") from exc

    # ------------------------------------------------------------------
    # Public query methods — same signatures as the original pyodbc version
    # ------------------------------------------------------------------

    def search_jobs(
        self,
        job_number_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
        client_filter: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        """
        Search jobs via the Azure Function API.

        Returns:
            List of job dicts with keys:
            job_number, job_date, job_description, job_name, job_type,
            client, location, work_status, year, instructing_person.
        """
        params = {"limit": limit}
        if job_number_filter:
            params["job_number"] = job_number_filter
        if status_filter:
            params["status"] = status_filter
        if client_filter:
            params["client"] = client_filter

        data = self._get("/api/getJobs", params=params)

        # Normalise field names from SQL column names to expected dict keys
        results = []
        for row in data:
            results.append(_normalise_job(row))

        log.info(
            "search_jobs: returned %d rows (number=%s status=%s client=%s)",
            len(results), job_number_filter, status_filter, client_filter,
        )
        return results

    def get_job(self, job_number: str) -> Optional[dict]:
        """
        Fetch a single job by exact job number.

        Tries the server-side job_number filter first, and falls back to
        fetching the recent list and filtering client-side if the filter
        endpoint errors out (observed 500 on some inputs).

        Returns:
            Job dict or None if not found.
        """
        if not job_number or not job_number.strip():
            raise RuntimeError("job_number must be a non-empty string")

        needle = job_number.strip()

        # Attempt 1: server-side filter (fast path).
        try:
            data = self._get("/api/getJobs", params={"job_number": needle})
        except RuntimeError as exc:
            log.warning(
                "get_job: server-side job_number filter failed (%s); "
                "falling back to client-side scan",
                exc,
            )
            data = self._get("/api/getJobs", params={"limit": 500})

        for row in data:
            normalised = _normalise_job(row)
            if normalised["job_number"] == needle:
                return normalised

        log.warning("get_job: job %s not found", job_number)
        return None

    def get_client_name(self, client_code: str) -> Optional[str]:
        """
        Fetch company name for a client code.
        Falls back to returning None if not available from the API.
        """
        if not client_code or not client_code.strip():
            return None

        try:
            data = self._get("/api/getClients", params={"client_code": client_code.strip()})
            if data and isinstance(data, list) and len(data) > 0:
                row = data[0]
                return row.get("CompanyName") or row.get("company_name")
        except RuntimeError as exc:
            # getClients endpoint may not exist yet — log and continue
            log.warning("get_client_name: API call failed (non-fatal): %s", exc)

        return None

    def get_work_statuses(self) -> List[str]:
        """
        Return distinct Work Status values from the API.
        """
        try:
            data = self._get("/api/getStatuses")
            if isinstance(data, list):
                return sorted(set(str(s) for s in data if s))
        except RuntimeError as exc:
            log.warning("get_work_statuses: API call failed: %s", exc)

        # Fallback — return common statuses if endpoint not yet available
        return ["Active", "Complete", "On Hold", "Cancelled"]


# ---------------------------------------------------------------------------
# Field normalisation
# ---------------------------------------------------------------------------

def _normalise_job(row: dict) -> dict:
    """
    Map SQL column names (as returned by mssql) to the expected dict keys.
    Handles both camelCase and original SQL column name formats.
    """
    from datetime import datetime

    def _parse_date(val):
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                return None
        return val

    job_date = _parse_date(
        row.get("Job Date") or row.get("job_date") or row.get("JobDate")
    )

    return {
        "job_number":         row.get("Job Number") or row.get("job_number") or row.get("JobNumber") or "",
        "job_date":           job_date,
        "job_description":    row.get("Job Description") or row.get("job_description") or row.get("JobDescription"),
        "job_name":           row.get("txtJobName") or row.get("job_name") or row.get("JobName"),
        "job_type":           row.get("JobType") or row.get("job_type"),
        "client":             row.get("Client") or row.get("client") or "",
        # Location for SiteDocs Address: use Job Description (fuller text than Locality)
        "location":           row.get("Job Description") or row.get("job_description") or row.get("JobDescription") or row.get("Locality") or row.get("location"),
        "work_status":        row.get("Work Status") or row.get("work_status") or row.get("WorkStatus"),
        "instructing_person": row.get("Instructing Person") or row.get("instructing_person"),
        "year":               job_date.year if job_date else None,
    }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_db_instance: Optional[LatitudeDB] = None


def get_db() -> LatitudeDB:
    """Return the process-level LatitudeDB singleton."""
    global _db_instance
    if _db_instance is None:
        _db_instance = LatitudeDB(_require_api_url())
    return _db_instance


def close_db() -> None:
    """Close and discard the singleton (called on shutdown)."""
    global _db_instance
    if _db_instance is not None:
        _db_instance.close()
        _db_instance = None
