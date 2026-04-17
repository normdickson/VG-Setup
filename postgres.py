"""
postgres.py — Connection + queries for the SiteDocs ETL Postgres DB.

Used by GET /search/details to return time tickets + forms for a given
Latitude job number. Forms are joined through the locations table where
LEFT(name, 6) = latitude.job_number.

Environment variables:
    POSTGRES_HOST       — hostname (e.g. your-server.postgres.database.azure.com)
    POSTGRES_DB         — database name (e.g. sitedocs)
    POSTGRES_USER       — username
    POSTGRES_PASSWORD   — password
    POSTGRES_PORT       — port (default 5432)
    POSTGRES_SSLMODE    — sslmode (default 'require' for Azure Flexible Server)

The pool is created lazily on first use and reused across requests.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

log = logging.getLogger("postgres")

_pool: Optional[ConnectionPool] = None


def _require(var: str) -> str:
    val = os.getenv(var)
    if not val:
        raise RuntimeError(
            f"{var} environment variable is not set. "
            "Cannot connect to SiteDocs Postgres."
        )
    return val


def _conninfo() -> str:
    """Build a libpq connection string from POSTGRES_* env vars."""
    host     = _require("POSTGRES_HOST")
    db       = _require("POSTGRES_DB")
    user     = _require("POSTGRES_USER")
    password = _require("POSTGRES_PASSWORD")
    port     = os.getenv("POSTGRES_PORT", "5432")
    sslmode  = os.getenv("POSTGRES_SSLMODE", "require")
    # Use keyword=value form; password is passed through libpq safely
    return (
        f"host={host} port={port} dbname={db} user={user} "
        f"password={password} sslmode={sslmode} "
        f"application_name=vg-setup"
    )


def get_pool() -> ConnectionPool:
    """Return the process-level connection pool (created lazily)."""
    global _pool
    if _pool is None:
        log.info("Opening Postgres pool to %s", os.getenv("POSTGRES_HOST"))
        _pool = ConnectionPool(
            conninfo   = _conninfo(),
            min_size   = 1,
            max_size   = 5,
            timeout    = 10,
            kwargs     = {"row_factory": dict_row},
            open       = True,
        )
    return _pool


def close_pool() -> None:
    """Close the pool on shutdown."""
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        finally:
            _pool = None


# ---------------------------------------------------------------------------
# Query: child records for a given Latitude job number
# ---------------------------------------------------------------------------

_DETAILS_SQL = """
SELECT json_build_object(
  'time_tickets', (
    SELECT COALESCE(json_agg(t ORDER BY ticket_date DESC NULLS LAST), '[]'::json)
    FROM (
      SELECT form_id, form_label, ticket_date, client,
             crew_chief, assistant,
             cc_total_hrs, sa_total_hrs, truck_km,
             details, approval, signed_by, signed_on
      FROM   time_tickets
      WHERE  job_no = %(job)s
        AND  NOT is_deleted
    ) t
  ),
  'forms', (
    SELECT COALESCE(json_agg(r ORDER BY last_modified_on DESC NULLS LAST), '[]'::json)
    FROM (
      SELECT f.id AS form_id,
             f.label AS form_label,
             ft.name AS form_type,
             l.name  AS location_name,
             f.creating_company_id,
             f.created_on,
             f.last_modified_on,
             f.due
      FROM   locations l
      JOIN   forms f           ON f.location_id = l.id
      LEFT JOIN form_types ft  ON ft.document_template_id = f.document_template_id
      WHERE  LEFT(l.name, 6) = %(job)s
        AND  NOT f.is_deleted
        AND  NOT l.is_archived
    ) r
  )
) AS details
"""


def get_job_details(job_number: str) -> dict:
    """
    Return { "time_tickets": [...], "forms": [...] } for the given 6-char
    Latitude job number.

    Raises:
        RuntimeError on connection/query failure.
    """
    pool = get_pool()
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_DETAILS_SQL, {"job": job_number})
            row = cur.fetchone()
    except Exception as exc:
        log.exception("get_job_details failed for %s", job_number)
        raise RuntimeError(f"Postgres query failed: {exc}") from exc

    # row["details"] is a dict (psycopg decodes json_build_object output)
    return row["details"] if row else {"time_tickets": [], "forms": []}


# ---------------------------------------------------------------------------
# Batch form counts — used by GET /search/counts to let the frontend hide
# expand chevrons on rows with zero form records.
# ---------------------------------------------------------------------------

_COUNTS_SQL = """
SELECT LEFT(l.name, 6) AS job_number,
       COUNT(*)::int   AS n
FROM   locations l
JOIN   forms     f ON f.location_id = l.id
WHERE  LEFT(l.name, 6) = ANY(%(jobs)s)
  AND  NOT f.is_deleted
  AND  NOT l.is_archived
GROUP  BY 1
"""


def get_form_counts(job_numbers: list[str]) -> dict[str, int]:
    """
    Return a mapping of job_number -> form count for the given list of
    6-char Latitude job numbers. Jobs with zero forms are omitted from
    the response (caller should treat missing keys as 0).
    """
    if not job_numbers:
        return {}

    pool = get_pool()
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_COUNTS_SQL, {"jobs": list(job_numbers)})
            rows = cur.fetchall()
    except Exception as exc:
        log.exception("get_form_counts failed for %d jobs", len(job_numbers))
        raise RuntimeError(f"Postgres counts query failed: {exc}") from exc

    return {r["job_number"]: r["n"] for r in rows}
