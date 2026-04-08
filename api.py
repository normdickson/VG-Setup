"""
api.py — Latitude Job Setup FastAPI service.

Endpoints:
    GET  /              → index.html (web UI)
    GET  /health        → liveness probe
    GET  /search        → query Latitude jobs
    GET  /statuses      → distinct Work Status values for UI drop-down
    POST /create        → batch provision: SharePoint folders + SiteDocs records

Environment variables required:
    LATITUDE_CONNECTION_STRING  — pyodbc connection string (no default; see latitude.py)
    GRAPH_TENANT_ID             — Azure AD tenant
    GRAPH_CLIENT_ID             — App registration client ID
    GRAPH_CLIENT_SECRET         — App registration secret
    SHAREPOINT_SITE_ID          — e.g. velocitygeomaticsinc.sharepoint.com
    SHAREPOINT_TEMPLATE_PATH    — e.g. Shared Documents/Job Files/Seed Folders
    SHAREPOINT_OUTPUT_BASE      — e.g. Shared Documents/Job Files
    SITEDOCS_API_KEY            — SiteDocs API key (no default; see sited.py)
    SITEDOCS_COMPANY_ID         — SiteDocs company UUID (has default)
    PORT                        — HTTP port (default 8000)
    LOG_LEVEL                   — DEBUG / INFO / WARNING (default INFO)
"""

from __future__ import annotations

import os
import uuid
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from models import (
    JobRecord,
    JobSearchResponse,
    WorkStatusesResponse,
    BatchCreationRequest,
    BatchCreationResponse,
    JobCreationResult,
)
import latitude
import sharepoint_helper
import sited

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("api")


# ---------------------------------------------------------------------------
# Lifespan (replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: nothing required (DB connects lazily on first request)
    log.info("Latitude Job Setup service starting")
    yield
    # Shutdown: close DB connection
    log.info("Latitude Job Setup service shutting down")
    latitude.close_db()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Latitude Job Setup",
    description=(
        "Provision jobs from Latitude: create SharePoint folders + "
        "SiteDocs location records — Velocity Geomatics Inc."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    """Serve the web UI."""
    return FileResponse(
        Path(__file__).parent / "index.html", media_type="text/html"
    )


@app.get("/health")
def health():
    """Liveness probe for container health checks."""
    return {"status": "ok", "service": "latitude-job-setup"}


@app.get("/search", response_model=JobSearchResponse)
def search_jobs(
    job_number: str | None = None,
    status:     str | None = None,
    client:     str | None = None,
    limit:      int        = 200,
):
    """
    Search Latitude jobs with optional filters.

    Query parameters:
        job_number  Prefix filter (e.g. "MSL" matches "MSL050439").
        status      Exact Work Status filter (e.g. "Active").
        client      Exact Client code filter.
        limit       Max results returned (1–500, default 200).
    """
    try:
        db = latitude.get_db()
        rows = db.search_jobs(
            job_number_filter=job_number,
            status_filter=status,
            client_filter=client,
            limit=limit,
        )
    except RuntimeError as exc:
        log.exception("search_jobs: DB error")
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        log.exception("search_jobs: unexpected error")
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")

    records = [
        JobRecord(
            job_number        = r["job_number"],
            job_date          = r["job_date"],
            job_description   = r["job_description"],
            job_name          = r["job_name"],
            job_type          = r["job_type"],
            client            = r["client"] or "",
            location          = r["location"],
            work_status       = r["work_status"],
            year              = r["year"] or datetime.now().year,
            instructing_person = r["instructing_person"],
        )
        for r in rows
    ]

    log.info("search_jobs: returned %d records", len(records))
    return JobSearchResponse(jobs=records, total=len(records))


@app.get("/statuses", response_model=WorkStatusesResponse)
def get_work_statuses():
    """
    Return distinct Work Status values from Latitude for the UI drop-down.
    This avoids hardcoding status values that may change in the source data.
    """
    try:
        db = latitude.get_db()
        statuses = db.get_work_statuses()
    except RuntimeError as exc:
        log.exception("get_work_statuses: DB error")
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        log.exception("get_work_statuses: unexpected error")
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")

    return WorkStatusesResponse(statuses=statuses)


@app.post("/create", response_model=BatchCreationResponse)
def create_job_batch(req: BatchCreationRequest):
    """
    Batch-provision jobs: create SharePoint folder + SiteDocs location for each.

    Behaviour:
    - Processes jobs in order.
    - Stops on the first failure; remaining jobs are not attempted.
    - No rollback of already-completed steps.

    Each job step:
    1. Fetch full job record from Latitude (verify exists, get year).
    2. Look up company name from tblClient.
    3. Copy Seed Folders template to Job Files/{year}/{job_number}/ in SharePoint.
    4. Create location record in SiteDocs.
    """
    batch_id   = str(uuid.uuid4())[:8]
    started_at = datetime.now(timezone.utc)

    log.info(
        "[%s] Batch create started: %d job(s)", batch_id, len(req.jobs)
    )

    results: list[JobCreationResult] = []

    # Verify DB is reachable before touching anything
    try:
        db = latitude.get_db()
        db._ensure_connection()   # explicit check so we fail fast
    except RuntimeError as exc:
        log.exception("[%s] Cannot connect to Latitude DB", batch_id)
        return BatchCreationResponse(
            batch_id       = batch_id,
            status         = "error",
            jobs_processed = [],
            started_at     = started_at,
            completed_at   = datetime.now(timezone.utc),
            failed_at_job  = None,
        )

    for idx, job_req in enumerate(req.jobs):
        job_number    = job_req.job_number.strip()
        client_code   = job_req.client_code.strip()
        location_name = job_req.location_name.strip()

        log.info(
            "[%s] Processing job %d/%d: %s",
            batch_id, idx + 1, len(req.jobs), job_number,
        )

        result = JobCreationResult(job_number=job_number, status="in_progress")
        results.append(result)

        try:
            # --- Step 1: Fetch job from Latitude ---
            job = db.get_job(job_number)
            if job is None:
                raise RuntimeError(
                    f"Job '{job_number}' was not found in Latitude. "
                    "Verify the job number and try again."
                )

            year         = job["year"] or datetime.now().year
            job_date_iso = (
                job["job_date"].isoformat() if job["job_date"] else None
            )
            log.info("[%s] %s: found in Latitude, year=%d", batch_id, job_number, year)

            # --- Step 2: Look up company name ---
            company_name = db.get_client_name(client_code)
            if company_name:
                log.info(
                    "[%s] %s: client %s → %s",
                    batch_id, job_number, client_code, company_name,
                )
            else:
                log.warning(
                    "[%s] %s: client code %s not found; Name will omit company",
                    batch_id, job_number, client_code,
                )

            # --- Step 3: Create SharePoint folder ---
            log.info(
                "[%s] %s: creating SharePoint folder Job Files/%d/%s …",
                batch_id, job_number, year, job_number,
            )
            sp_folder_url = sharepoint_helper.copy_template_folder(job_number, year)

            result.sp_folder_url  = sp_folder_url
            result.sp_folder_path = f"Job Files/{year}/{job_number}"
            log.info(
                "[%s] %s: SharePoint folder created → %s",
                batch_id, job_number, sp_folder_url,
            )

            # --- Step 4: Create SiteDocs location ---
            log.info("[%s] %s: creating SiteDocs location …", batch_id, job_number)
            sitedocs_id = sited.create_location(
                job_number      = job_number,
                job_description = job["job_description"],
                location        = location_name,
                job_date_iso    = job_date_iso,
                company_name    = company_name,
            )

            result.sitedocs_id  = sitedocs_id
            result.sitedocs_url = f"https://app.sitedocs.com/locations/{sitedocs_id}"
            result.status       = "success"

            log.info(
                "[%s] %s: complete — SiteDocs id=%s",
                batch_id, job_number, sitedocs_id,
            )

        except Exception as exc:
            log.exception("[%s] %s: FAILED", batch_id, job_number)
            result.status = "error"
            result.error  = str(exc)

            return BatchCreationResponse(
                batch_id       = batch_id,
                status         = "error",
                jobs_processed = results,
                failed_at_job  = job_number,
                started_at     = started_at,
                completed_at   = datetime.now(timezone.utc),
            )

    # All jobs succeeded
    log.info("[%s] Batch complete: %d/%d succeeded", batch_id, len(results), len(req.jobs))
    return BatchCreationResponse(
        batch_id       = batch_id,
        status         = "success",
        jobs_processed = results,
        started_at     = started_at,
        completed_at   = datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    log.info("Starting on port %d", port)
    uvicorn.run(
        "api:app",
        host      = "0.0.0.0",
        port      = port,
        reload    = False,
        log_level = os.environ.get("LOG_LEVEL", "info").lower(),
    )
