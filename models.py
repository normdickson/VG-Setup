"""
models.py — Pydantic request / response models.

All models target Pydantic v2 (as specified in requirements.txt).

Changes from v1:
- List field length constraints use min_length / max_length (not min_items / max_items).
- datetime.utcnow() replaced with datetime.now(timezone.utc) (deprecated in 3.12).
- Unused models (JobSearchFilter, JobCreationStep) removed.
- JobRecord extended with job_name and job_type fields (requested in requirements).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class JobRecord(BaseModel):
    """A single job row returned from Latitude."""

    job_number:        str
    job_date:          Optional[datetime]  = None
    job_description:   Optional[str]       = None
    job_name:          Optional[str]       = None   # txtJobName
    job_type:          Optional[str]       = None   # JobType
    client:            str
    location:          Optional[str]       = None
    work_status:       Optional[str]       = None
    year:              int
    instructing_person: Optional[str]      = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_number":        "MSL050439",
                "job_date":          "2026-02-21T00:00:00",
                "job_description":   "Pipeline survey at Cenovus site",
                "job_name":          "Cenovus Pipeline ROW",
                "job_type":          "Survey",
                "client":            "CEV001",
                "location":          "Peace River, AB",
                "work_status":       "Active",
                "year":              2026,
                "instructing_person": "Jane Smith",
            }
        }
    }


class JobSearchResponse(BaseModel):
    """Response from GET /search."""

    jobs:  List[JobRecord]
    total: int


class WorkStatusesResponse(BaseModel):
    """Response from GET /statuses — used to populate the UI drop-down."""

    statuses: List[str]


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------

class JobCreationRequest(BaseModel):
    """
    Single job submission for batch provisioning.

    location_name is required and should be a real address or locality.
    The UI must not allow submission with an empty or placeholder value.
    """

    job_number:    str = Field(..., max_length=50,  description="Job number from Latitude")
    client_code:   str = Field(..., max_length=50,  description="Client code from Latitude")
    location_name: str = Field(..., max_length=255, description="Address / locality for SiteDocs")

    model_config = {
        "json_schema_extra": {
            "example": {
                "job_number":    "MSL050439",
                "client_code":   "CEV001",
                "location_name": "Peace River, AB",
            }
        }
    }


class BatchCreationRequest(BaseModel):
    """Batch submission — 1 to 50 jobs per request."""

    # Pydantic v2: use min_length / max_length for List constraints
    jobs: List[JobCreationRequest] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Jobs to provision (1–50 per batch)",
    )


class JobCreationResult(BaseModel):
    """Per-job result within a batch response."""

    job_number:     str
    status:         str = Field(
        ..., description="One of: 'success', 'in_progress', 'error'"
    )
    sp_folder_url:  Optional[str] = None
    sp_folder_path: Optional[str] = None
    sitedocs_id:    Optional[str] = None
    sitedocs_url:   Optional[str] = None
    error:          Optional[str] = None
    timestamp:      datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class BatchCreationResponse(BaseModel):
    """Response from POST /create."""

    batch_id:       str
    status:         str = Field(
        ..., description="One of: 'success', 'error'"
    )
    jobs_processed: List[JobCreationResult]
    failed_at_job:  Optional[str] = None   # job_number that caused the halt
    started_at:     datetime
    completed_at:   Optional[datetime] = None
