"""
sharepoint_helper.py — SharePoint folder operations via Microsoft Graph API.

Key design decisions:
- Folder copy uses the Graph API /copy endpoint (server-side, single API call)
  rather than downloading every file into memory and re-uploading.  The /copy
  call is asynchronous on Microsoft's side; we poll the returned Location URL
  until the operation completes or times out.
- Year folder and job folder are created (if absent) BEFORE the copy is
  triggered, with clear errors on conflict.
- All folder-list operations follow @odata.nextLink pagination so template
  folders with >200 items are handled correctly.
- Token management, site/drive resolution, and connection settings are
  identical in pattern to the vg-map sharepoint.py module.
"""

from __future__ import annotations

import os
import time
import logging
import threading
from typing import Optional

import requests

log = logging.getLogger("sharepoint")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TENANT_ID     = os.environ.get("GRAPH_TENANT_ID", "")
CLIENT_ID     = os.environ.get("GRAPH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")
SP_HOST       = os.environ.get("SHAREPOINT_SITE_ID", "velocitygeomaticsinc.sharepoint.com")

# SharePoint paths (relative to site, same convention as vg-map sharepoint.py)
# e.g. "Shared Documents/Job Files/Seed Folders"
TEMPLATE_PATH = os.environ.get(
    "SHAREPOINT_TEMPLATE_PATH", "Shared Documents/Job Files/Seed Folders"
)
# e.g. "Shared Documents/Job Files"
OUTPUT_BASE = os.environ.get("SHAREPOINT_OUTPUT_BASE", "Shared Documents/Job Files")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

# How long to poll for the async /copy operation (seconds)
COPY_POLL_TIMEOUT  = 300   # 5 minutes
COPY_POLL_INTERVAL = 5     # seconds between polls

# ---------------------------------------------------------------------------
# Token caching (thread-safe)
# ---------------------------------------------------------------------------

_token_lock    = threading.Lock()
_access_token: Optional[str] = None
_token_expiry: float = 0.0


def _get_token() -> str:
    global _access_token, _token_expiry
    with _token_lock:
        if _access_token and time.time() < _token_expiry - 60:
            return _access_token

        log.info("Fetching Graph API token")
        resp = requests.post(TOKEN_URL, data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope":         "https://graph.microsoft.com/.default",
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        _access_token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 3600)
        return _access_token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}"}


# ---------------------------------------------------------------------------
# Site & drive resolution (cached)
# ---------------------------------------------------------------------------

_site_id:  Optional[str] = None
_drive_id: Optional[str] = None


def _get_site_id() -> str:
    global _site_id
    if _site_id:
        return _site_id

    url = f"{GRAPH_BASE}/sites/{SP_HOST}"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    _site_id = resp.json()["id"]
    log.info("Resolved site ID: %s", _site_id)
    return _site_id


def _get_drive_id() -> str:
    global _drive_id
    if _drive_id:
        return _drive_id

    site_id = _get_site_id()
    url = f"{GRAPH_BASE}/sites/{site_id}/drives"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    drives = resp.json().get("value", [])

    for d in drives:
        if d.get("name", "").lower() in ("documents", "shared documents"):
            _drive_id = d["id"]
            log.info("Resolved drive ID: %s (%s)", _drive_id, d["name"])
            return _drive_id

    # Fallback: first drive
    _drive_id = drives[0]["id"]
    log.warning("Drive name not matched; using first drive (fallback): %s", _drive_id)
    return _drive_id


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _encode(path: str) -> str:
    """URL-encode spaces in a path segment for Graph API."""
    return path.replace(" ", "%20")


def _get_item(path: str) -> Optional[dict]:
    """
    Return the Graph driveItem dict for *path*, or None if it does not exist.
    Raises on any error other than 404.
    """
    drive_id = _get_drive_id()
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{_encode(path)}"
    resp = requests.get(url, headers=_headers(), timeout=30)

    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _create_folder_in_parent(parent_path: str, folder_name: str) -> dict:
    """
    Create *folder_name* directly inside *parent_path*.
    Uses conflictBehavior=fail so that re-submitting a job raises a clear
    error rather than silently creating MSL050439_1, MSL050439_2, …

    Returns the newly created driveItem dict.
    Raises RuntimeError if the folder already exists or on API error.
    """
    drive_id = _get_drive_id()
    encoded_parent = _encode(parent_path)
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded_parent}:/children"

    payload = {
        "name":   folder_name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }

    resp = requests.post(
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )

    if resp.status_code == 409:
        raise RuntimeError(
            f"Folder already exists: {parent_path}/{folder_name}. "
            "Delete it manually or choose a different job number."
        )

    resp.raise_for_status()
    item = resp.json()
    log.info("Created folder: %s/%s (id=%s)", parent_path, folder_name, item["id"])
    return item


def _ensure_folder(path: str) -> str:
    """
    Ensure the folder at *path* exists, creating it if absent.
    Creates only the leaf folder; assumes the parent already exists.

    Returns the folder's driveItem ID.
    Raises RuntimeError on API error.
    """
    item = _get_item(path)
    if item:
        if "folder" not in item:
            raise RuntimeError(f"Path exists but is not a folder: {path}")
        log.debug("Folder already exists: %s", path)
        return item["id"]

    # Create the leaf
    parts = path.rstrip("/").split("/")
    parent_path  = "/".join(parts[:-1])
    folder_name  = parts[-1]
    new_item = _create_folder_in_parent(parent_path, folder_name)
    return new_item["id"]


def _list_folder_items_all(folder_path: str) -> list[dict]:
    """
    Return ALL driveItems (files and folders) in *folder_path*.
    Follows @odata.nextLink pagination so folders with >200 items are
    handled correctly.
    """
    drive_id = _get_drive_id()
    url: Optional[str] = (
        f"{GRAPH_BASE}/drives/{drive_id}/root:/{_encode(folder_path)}:/children"
    )

    items: list[dict] = []
    while url:
        resp = requests.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")   # None when on the last page

    return items


# ---------------------------------------------------------------------------
# Server-side copy with async polling
# ---------------------------------------------------------------------------

def _server_side_copy(
    src_item_id:    str,
    dest_parent_id: str,
    dest_name:      str,
) -> None:
    """
    Trigger a server-side recursive copy of *src_item_id* into
    *dest_parent_id*, naming the result *dest_name*.

    Graph /copy is asynchronous: it returns HTTP 202 with a Location header
    pointing to a monitor URL.  We poll that URL until the status is
    "completed" or we reach COPY_POLL_TIMEOUT seconds.

    Raises:
        RuntimeError if the copy fails, is cancelled, or times out.
    """
    drive_id = _get_drive_id()
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{src_item_id}/copy"

    payload = {
        "parentReference": {
            "driveId": drive_id,
            "id":      dest_parent_id,
        },
        "name": dest_name,
    }

    resp = requests.post(
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )

    if resp.status_code not in (200, 202):
        resp.raise_for_status()

    monitor_url: Optional[str] = resp.headers.get("Location")
    if not monitor_url:
        # Some tenants return 200 with the completed item immediately
        log.info("Copy completed synchronously for: %s", dest_name)
        return

    log.info("Copy operation started for %s; polling monitor URL …", dest_name)

    deadline = time.monotonic() + COPY_POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(COPY_POLL_INTERVAL)

        poll = requests.get(monitor_url, timeout=30)
        poll.raise_for_status()
        data = poll.json()
        status = data.get("status", "").lower()

        log.debug("Copy status for %s: %s", dest_name, status)

        if status == "completed":
            log.info("Server-side copy completed: %s", dest_name)
            return

        if status in ("failed", "cancelled"):
            detail = data.get("error", {}).get("message", str(data))
            raise RuntimeError(
                f"SharePoint copy operation {status} for '{dest_name}': {detail}"
            )
        # "notStarted" or "inProgress" — keep polling

    raise RuntimeError(
        f"SharePoint copy timed out after {COPY_POLL_TIMEOUT}s for '{dest_name}'"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def copy_template_folder(job_number: str, year: int) -> str:
    """
    Recursively copy the Seed Folders template to
    ``{OUTPUT_BASE}/{year}/{job_number}/`` using a single server-side
    Graph API /copy call.

    Pre-conditions checked before any mutation:
    - Template folder exists and is a folder.
    - Year folder is created if it does not exist.
    - Job folder must NOT already exist (raises RuntimeError on conflict).

    Args:
        job_number: Job identifier used as the destination folder name.
        year:       Calendar year, used as an intermediate path segment.

    Returns:
        SharePoint webUrl of the newly created job folder.

    Raises:
        RuntimeError on any API error, missing template, or duplicate job.
    """
    base         = OUTPUT_BASE.rstrip("/")
    year_path    = f"{base}/{year}"
    job_path     = f"{year_path}/{job_number}"

    log.info(
        "copy_template_folder: job=%s year=%d → %s", job_number, year, job_path
    )

    try:
        # 1. Verify the template folder exists
        template_item = _get_item(TEMPLATE_PATH)
        if template_item is None:
            raise RuntimeError(
                f"Template folder not found in SharePoint: '{TEMPLATE_PATH}'. "
                "Check the SHAREPOINT_TEMPLATE_PATH environment variable."
            )
        if "folder" not in template_item:
            raise RuntimeError(
                f"Template path exists but is not a folder: '{TEMPLATE_PATH}'"
            )
        template_id = template_item["id"]
        log.info("Template folder confirmed (id=%s)", template_id)

        # 2. Ensure the year folder exists (create silently if absent)
        year_folder_id = _ensure_folder(year_path)
        log.info("Year folder ready: %s (id=%s)", year_path, year_folder_id)

        # 3. Check the job folder does NOT already exist
        existing = _get_item(job_path)
        if existing is not None:
            raise RuntimeError(
                f"Job folder already exists in SharePoint: '{job_path}'. "
                "If this is a re-submission, check SiteDocs before proceeding."
            )

        # 4. Create the empty job folder first
        job_folder = _create_folder_in_parent(year_path, job_number)
        job_folder_id = job_folder["id"]
        log.info("Job folder created: %s (id=%s)", job_path, job_folder_id)

        # 5. Copy each child of the template INTO the job folder.
        #    This copies the CONTENTS of Seed Folders, not the folder itself.
        template_children = _list_folder_items_all(TEMPLATE_PATH)
        if not template_children:
            log.warning("Template folder '%s' is empty — no items to copy", TEMPLATE_PATH)

        for child in template_children:
            child_name = child.get("name", "unknown")
            log.info("  copying template item: %s", child_name)
            _server_side_copy(
                src_item_id    = child["id"],
                dest_parent_id = job_folder_id,
                dest_name      = child_name,
            )

        # 6. Retrieve the web URL of the newly created folder
        job_item = _get_item(job_path)
        if job_item is None:
            raise RuntimeError(
                f"Copy appeared to succeed but the job folder is not visible "
                f"at '{job_path}'. SharePoint may still be indexing it."
            )

        web_url = job_item.get("webUrl", "")
        log.info("Job folder created: %s → %s", job_path, web_url)
        return web_url

    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"SharePoint API error for job {job_number}: {exc}") from exc


def get_folder_url(year: int, job_number: str) -> Optional[str]:
    """
    Return the SharePoint webUrl for an existing job folder, or None.
    Does NOT create anything.
    """
    job_path = f"{OUTPUT_BASE.rstrip('/')}/{year}/{job_number}"
    item = _get_item(job_path)
    if item:
        return item.get("webUrl")
    return None
