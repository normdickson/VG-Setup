"""
models.py — Pydantic request / response models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, List

from pydantic import BaseModel, Field


class JobRecord(BaseModel):
    job_number:         str
    job_date:           Optional[datetime] = None
    job_description:    Optional[str]      = None
    job_name:           Optional[str]      = None
    job_type:           Optional[str]      = None
    client:             str
    company_name:       Optional[str]      = None
    location:           Optional[str]      = None
    work_status:        Optional[str]      = None
    year:               int
    instructing_person: Optional[str]      = None
    provisioned_date:   Optional[datetime] = None  # dteJobUserField25


class JobSearchResponse(BaseModel):
    jobs:  List[JobRecord]
    total: int


class WorkStatusesResponse(BaseModel):
    statuses: List[str]


class JobCreationRequest(BaseModel):
    job_number:        str  = Field(..., max_length=50)
    client_code:       str  = Field(..., max_length=50)
    location_name:     str  = Field(..., max_length=255)
    create_sharepoint: bool = Field(True,  description="Create SharePoint folder")
    create_sitedocs:   bool = Field(True,  description="Create SiteDocs location")


class BatchCreationRequest(BaseModel):
    jobs: List[JobCreationRequest] = Field(..., min_length=1, max_length=50)


class JobCreationResult(BaseModel):
    job_number:     str
    status:         str
    sp_folder_url:  Optional[str] = None
    sp_folder_path: Optional[str] = None
    sitedocs_id:    Optional[str] = None
    sitedocs_url:   Optional[str] = None
    error:          Optional[str] = None
    timestamp:      datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BatchCreationResponse(BaseModel):
    batch_id:       str
    status:         str
    jobs_processed: List[JobCreationResult]
    failed_at_job:  Optional[str]     = None
    started_at:     datetime
    completed_at:   Optional[datetime] = None
