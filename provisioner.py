"""
provisioner.py — Polling worker that auto-provisions new Latitude jobs.

Runs as a one-shot script: queries Latitude for jobs that are NOT yet
provisioned, filters by a safety window (created within last N days),
and for each qualifying job creates the SharePoint folder and SiteDocs
location via the same modules used by the /create API endpoint.

Designed to be run in three ways:
  1. On a schedule (Azure Container Apps Job, GitHub cron, Task Scheduler)
     — config from env vars.
  2. Ad-hoc from the CLI with flags (flags override env vars).
  3. To provision one or more SPECIFIC jobs by number (--job MSL050439),
     bypassing the lookback / status filters entirely.

Environment variables (all optional; CLI flags override):
    POLL_LOOKBACK_DAYS      Only process jobs with job_date within the last N
                            days (default: 7).
    POLL_JOB_PREFIX         Optional job-number prefix filter (e.g. "MSL").
    POLL_DRY_RUN            "1" to log what WOULD happen without making API
                            calls. Defaults to "0".
    POLL_MAX_JOBS           Safety cap on how many jobs a single run can
                            provision (default: 25).
    POLL_DEFAULT_LOCATION   Fallback address if a job has no Locality.

CLI usage:
    # Show what the scheduled run would do, no changes
    python provisioner.py --dry-run

    # Only MSL jobs in the last 30 days, up to 10 of them
    python provisioner.py --prefix MSL --lookback 30 --max 10

    # Force-provision specific jobs (ignores filters & lookback window)
    python provisioner.py --job MSL050439 --job MSL050440

    # Set a default location for jobs that don't have one
    python provisioner.py --default-location "Calgary, AB"

Exit codes:
    0  — success (including "nothing to do")
    1  — any job failed; see logs for details
    2  — fatal error (DB unreachable, config missing)
"""

from __future__ import annotations

import argparse
import os
import sys
import logging
from datetime import datetime, timezone, timedelta

import latitude
import sharepoint_helper
import sited
import notify

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("provisioner")


# ---------------------------------------------------------------------------
# Config — populated from env vars, may be overridden by CLI flags in main()
# ---------------------------------------------------------------------------

LOOKBACK_DAYS    = int(os.environ.get("POLL_LOOKBACK_DAYS", "7"))
JOB_PREFIX       = os.environ.get("POLL_JOB_PREFIX", "")
DRY_RUN          = os.environ.get("POLL_DRY_RUN", "0") == "1"
MAX_JOBS         = int(os.environ.get("POLL_MAX_JOBS", "25"))
DEFAULT_LOCATION = os.environ.get("POLL_DEFAULT_LOCATION", "").strip()
EXPLICIT_JOBS: list[str] = []   # set by --job flags; skips candidate filtering


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def candidate_jobs() -> list[dict]:
    """Return jobs that should be provisioned on this run."""
    db = latitude.get_db()

    # Explicit job-number mode: fetch each by exact number, skip filters.
    if EXPLICIT_JOBS:
        out: list[dict] = []
        for number in EXPLICIT_JOBS:
            row = db.get_job(number)
            if row is None:
                log.error("--job %s: not found in Latitude", number)
                continue
            if not row.get("location") and not DEFAULT_LOCATION:
                log.error("--job %s: has no location; pass --default-location or set one in Latitude", number)
                continue
            out.append(row)
        return out

    # Normal polling mode: find unprovisioned jobs inside the lookback window.
    rows = db.search_jobs(
        job_number_filter=JOB_PREFIX or None,
        status_filter=None,
        client_filter=None,
        limit=500,
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    out = []

    for row in rows:
        # Already provisioned?
        if row.get("provisioned_date"):
            continue

        # Inside lookback window?
        job_date = row.get("job_date")
        if not job_date:
            log.debug("skip %s: no job_date", row.get("job_number"))
            continue
        if job_date.tzinfo is None:
            job_date = job_date.replace(tzinfo=timezone.utc)
        if job_date < cutoff:
            log.debug("skip %s: outside %d-day window", row.get("job_number"), LOOKBACK_DAYS)
            continue

        # Location present (or fallback available)?
        if not row.get("location") and not DEFAULT_LOCATION:
            log.warning("skip %s: no location and no POLL_DEFAULT_LOCATION", row.get("job_number"))
            continue

        out.append(row)

    return out[:MAX_JOBS]


def provision_one(job: dict) -> dict:
    """
    Provision a single job. Raises on failure.

    Returns a dict summarising what was created, for use in notifications:
        {
          "job_number":      str,
          "company_name":    str | None,
          "location":        str,
          "sp_url":          str | None,
          "sitedocs_id":     str | None,
          "sitedocs_url":    str | None,
        }
    """
    db          = latitude.get_db()
    job_number  = job["job_number"]
    client_code = job["client"]
    year        = job["year"] or datetime.now().year
    location    = job["location"] or DEFAULT_LOCATION
    job_date    = job["job_date"].isoformat() if job["job_date"] else None

    company_name = db.get_client_name(client_code)

    summary = {
        "job_number":   job_number,
        "company_name": company_name,
        "location":     location,
        "sp_url":       None,
        "sitedocs_id":  None,
        "sitedocs_url": None,
    }

    if DRY_RUN:
        log.info("DRY_RUN: would provision %s (client=%s, location=%s)",
                 job_number, company_name or client_code, location)
        return summary

    log.info("provisioning %s (client=%s, year=%d)", job_number, company_name or client_code, year)

    # 1) SharePoint folder
    sp_url = sharepoint_helper.copy_template_folder(job_number, year)
    summary["sp_url"] = sp_url
    log.info("  SharePoint folder created: %s", sp_url)

    # 2) SiteDocs location
    sitedocs_id = sited.create_location(
        job_number      = job_number,
        job_description = job.get("job_description"),
        location        = location,
        job_date_iso    = job_date,
        company_name    = company_name,
    )
    summary["sitedocs_id"]  = sitedocs_id
    summary["sitedocs_url"] = f"https://app.sitedocs.com/locations/{sitedocs_id}" if sitedocs_id else None
    log.info("  SiteDocs location created: id=%s", sitedocs_id)

    # 3) Stamp dteJobUserField25 so the poller won't re-provision next run.
    try:
        if db.mark_provisioned(job_number):
            log.info("  marked provisioned in Latitude (dteJobUserField25)")
    except Exception as exc:
        # Provisioning artifacts were created successfully; failing to stamp
        # the date is a soft error — log loudly but don't raise, otherwise the
        # caller will think the whole job failed.
        log.error("  WARNING: could not mark %s as provisioned: %s", job_number, exc)

    return summary


def _build_email_body(successes: list[dict]) -> str:
    """Render an HTML summary of successful provisions."""
    rows = []
    for s in successes:
        company = s.get("company_name") or "—"
        loc     = s.get("location")     or "—"
        sp      = s.get("sp_url")
        sd      = s.get("sitedocs_url")
        links = []
        if sp: links.append(f'<a href="{sp}">SharePoint folder</a>')
        if sd: links.append(f'<a href="{sd}">SiteDocs location</a>')
        link_html = " &nbsp;·&nbsp; ".join(links) if links else "—"

        rows.append(
            f"<tr>"
            f"<td style='padding:6px 12px;font-weight:600;'>{s['job_number']}</td>"
            f"<td style='padding:6px 12px;'>{company}</td>"
            f"<td style='padding:6px 12px;'>{loc}</td>"
            f"<td style='padding:6px 12px;'>{link_html}</td>"
            f"</tr>"
        )

    table = (
        "<table style='border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;font-size:13px;'>"
        "<thead><tr style='background:#142644;color:#fff;'>"
        "<th style='padding:6px 12px;text-align:left;'>Job</th>"
        "<th style='padding:6px 12px;text-align:left;'>Client</th>"
        "<th style='padding:6px 12px;text-align:left;'>Location</th>"
        "<th style='padding:6px 12px;text-align:left;'>Links</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )

    return (
        "<p style='font-family:Segoe UI,Arial,sans-serif;font-size:14px;'>"
        f"Auto-provisioned <b>{len(successes)}</b> new job(s) from Latitude:"
        "</p>" + table +
        "<p style='font-family:Segoe UI,Arial,sans-serif;font-size:11px;color:#888;margin-top:18px;'>"
        "Sent by VG-Setup provisioner. "
        "To stop these emails, unset NOTIFY_EMAIL_TO in run-provisioner.ps1 "
        "or the Container App configuration."
        "</p>"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI flags. Any flag passed overrides the matching env-var default."""
    p = argparse.ArgumentParser(
        prog="provisioner",
        description="Auto-provision new Latitude jobs (SharePoint + SiteDocs).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  provisioner.py --dry-run\n"
            "  provisioner.py --prefix MSL --lookback 30 --max 10\n"
            "  provisioner.py --job MSL050439 --job MSL050440\n"
            "  provisioner.py --default-location 'Calgary, AB'\n"
        ),
    )
    p.add_argument("--lookback", type=int, metavar="DAYS",
                   help=f"Only process jobs within last N days (default: {LOOKBACK_DAYS})")
    p.add_argument("--prefix", metavar="TEXT",
                   help=f"Job-number prefix filter (default: '{JOB_PREFIX}')")
    p.add_argument("--max", type=int, metavar="N", dest="max_jobs",
                   help=f"Max jobs per run (default: {MAX_JOBS})")
    p.add_argument("--default-location", metavar="ADDR",
                   help="Fallback address for jobs with no location")
    p.add_argument("--job", action="append", metavar="NUMBER", default=[],
                   help="Specific job number to provision (can repeat); "
                        "bypasses lookback and status filters")
    p.add_argument("--dry-run", action="store_true",
                   help="Log actions without calling any APIs")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG-level logging")
    return p.parse_args(argv)


def apply_args(args: argparse.Namespace) -> None:
    """Push parsed CLI args into module-level config."""
    global LOOKBACK_DAYS, JOB_PREFIX, MAX_JOBS, DEFAULT_LOCATION
    global DRY_RUN, EXPLICIT_JOBS

    if args.lookback is not None:         LOOKBACK_DAYS    = args.lookback
    if args.prefix is not None:           JOB_PREFIX       = args.prefix
    if args.max_jobs is not None:         MAX_JOBS         = args.max_jobs
    if args.default_location is not None: DEFAULT_LOCATION = args.default_location.strip()
    if args.dry_run:                      DRY_RUN          = True
    if args.job:                          EXPLICIT_JOBS    = [j.strip() for j in args.job if j.strip()]

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply_args(args)

    if EXPLICIT_JOBS:
        log.info(
            "provisioner starting in EXPLICIT mode: jobs=%s (dry_run=%s)",
            ", ".join(EXPLICIT_JOBS), DRY_RUN,
        )
    else:
        log.info(
            "provisioner starting (lookback=%dd, prefix=%s, dry_run=%s, max=%d)",
            LOOKBACK_DAYS, JOB_PREFIX or "*", DRY_RUN, MAX_JOBS,
        )

    try:
        jobs = candidate_jobs()
    except Exception as exc:
        log.exception("failed to query Latitude: %s", exc)
        return 2

    if not jobs:
        log.info("no candidate jobs — nothing to do")
        return 0

    log.info("found %d candidate job(s): %s",
             len(jobs), ", ".join(j["job_number"] for j in jobs))

    successes: list[dict] = []
    failures = 0
    for job in jobs:
        try:
            summary = provision_one(job)
            # Only count as a real success if we actually created something.
            if not DRY_RUN and (summary.get("sp_url") or summary.get("sitedocs_id")):
                successes.append(summary)
        except Exception as exc:
            failures += 1
            log.exception("FAILED to provision %s: %s", job["job_number"], exc)

    log.info("done: %d ok, %d failed", len(jobs) - failures, failures)

    # Send summary email if anything was actually provisioned.
    if successes:
        subject = (
            f"VG-Setup: {len(successes)} job(s) auto-provisioned"
            + (f" — {successes[0]['job_number']}" if len(successes) == 1 else "")
        )
        try:
            notify.send_email(subject, _build_email_body(successes))
        except Exception as exc:
            log.warning("email notification failed (non-fatal): %s", exc)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
