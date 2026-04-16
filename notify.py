"""
notify.py — Send Graph API emails.

Reuses the app registration already used by sharepoint_helper, so no new
credentials are required. Uses the /users/{id|upn}/sendMail endpoint with
application permissions (Mail.Send).

Environment variables:
    GRAPH_TENANT_ID       Azure AD tenant (already set for SharePoint)
    GRAPH_CLIENT_ID       App registration client ID (already set)
    GRAPH_CLIENT_SECRET   App secret (already set)
    NOTIFY_EMAIL_FROM     Mailbox to send from (must exist in tenant).
                          e.g. "alerts@velocitygeomatics.ca" or a service
                          account. Required.
    NOTIFY_EMAIL_TO       Recipient(s). Comma-separate for multiple.
                          e.g. "norm.dickson@magnussolutions.ca"

Required Graph API permission on the app registration:
    Mail.Send  (Application) + admin consent.

If NOTIFY_EMAIL_FROM or NOTIFY_EMAIL_TO is unset, send_email() is a no-op
(warns and returns False) so notifications are opt-in.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

import requests

log = logging.getLogger("notify")

_GRAPH_BASE  = "https://graph.microsoft.com/v1.0"
_LOGIN_BASE  = "https://login.microsoftonline.com"


def _get_token() -> Optional[str]:
    tenant = os.environ.get("GRAPH_TENANT_ID")
    client = os.environ.get("GRAPH_CLIENT_ID")
    secret = os.environ.get("GRAPH_CLIENT_SECRET")
    if not (tenant and client and secret):
        log.warning("notify: Graph credentials missing; email disabled")
        return None

    try:
        resp = requests.post(
            f"{_LOGIN_BASE}/{tenant}/oauth2/v2.0/token",
            data={
                "client_id":     client,
                "client_secret": secret,
                "scope":         "https://graph.microsoft.com/.default",
                "grant_type":    "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as exc:
        log.warning("notify: could not get Graph token: %s", exc)
        return None


def _recipients(raw: str) -> list[dict]:
    return [
        {"emailAddress": {"address": a.strip()}}
        for a in raw.split(",")
        if a.strip()
    ]


def send_email(subject: str, html_body: str) -> bool:
    """
    Send an HTML email via Graph.

    Returns True on success, False on any failure (never raises — failing to
    notify should not fail the calling process).
    """
    mail_from = os.environ.get("NOTIFY_EMAIL_FROM", "").strip()
    mail_to   = os.environ.get("NOTIFY_EMAIL_TO",   "").strip()

    if not mail_from or not mail_to:
        log.info("notify: NOTIFY_EMAIL_FROM or NOTIFY_EMAIL_TO not set; skipping")
        return False

    token = _get_token()
    if not token:
        return False

    payload = {
        "message": {
            "subject":      subject,
            "body":         {"contentType": "HTML", "content": html_body},
            "toRecipients": _recipients(mail_to),
        },
        "saveToSentItems": False,
    }

    try:
        resp = requests.post(
            f"{_GRAPH_BASE}/users/{mail_from}/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code == 202:
            log.info("notify: email sent to %s", mail_to)
            return True
        log.warning(
            "notify: sendMail returned %d: %s",
            resp.status_code, resp.text[:300],
        )
        return False
    except Exception as exc:
        log.warning("notify: sendMail failed: %s", exc)
        return False
