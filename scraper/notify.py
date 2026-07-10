"""Gmail SMTP notifier.

Credentials come from env vars (which the workflow wires up from GitHub Secrets):

    GMAIL_USER           - your Gmail address
    GMAIL_APP_PASSWORD   - a 16-char App Password (requires 2FA on the account)
    NOTIFY_TO            - optional; comma-separated list of recipient
                           addresses. Defaults to GMAIL_USER.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


# Section order and headings for the digest. LinkedIn postings are split by
# how you apply: direct (company careers site) vs Easy Apply (LinkedIn-only).
_SECTIONS = [
    ("direct", "LINKEDIN - DIRECT APPLY (company site)"),
    ("easy_apply", "LINKEDIN - EASY APPLY (LinkedIn only)"),
    ("unknown", "LINKEDIN - APPLY TYPE UNKNOWN"),
    ("indeed", "INDEED"),
]


def _format_job(j: dict) -> str:
    locations = j.get("locations") or [j.get("location", "")]
    loc_str = "; ".join(loc for loc in locations if loc)
    lines = [
        f"{j.get('title', '(no title)')} @ {j.get('company', '(unknown)')} ({loc_str})",
        j.get("url", ""),
    ]
    if j.get("apply_url"):
        lines.append(f"Apply directly: {j['apply_url']}")
    return "\n".join(lines)


def _run_timestamp() -> str:
    now = datetime.now(timezone.utc)
    pacific = now.astimezone(ZoneInfo("America/Los_Angeles"))
    return (
        f"Scraper ran at {pacific.strftime('%Y-%m-%d %I:%M %p %Z')} "
        f"({now.strftime('%Y-%m-%d %H:%M UTC')})"
    )


def _format_body(new_jobs: list[dict]) -> str:
    grouped: dict[str, list[dict]] = {}
    for j in new_jobs:
        if j.get("source") == "linkedin":
            key = j.get("apply_type", "unknown")
        else:
            key = j.get("source", "unknown")
        grouped.setdefault(key, []).append(j)

    parts: list[str] = [_run_timestamp(), ""]
    for key, heading in _SECTIONS:
        jobs = grouped.pop(key, None)
        if not jobs:
            continue
        parts.append(f"=== {heading} ({len(jobs)}) ===")
        parts.extend(_format_job(j) for j in jobs)
        parts.append("")
    # Anything from a source/type not covered above.
    for key, jobs in grouped.items():
        parts.append(f"=== {key.upper()} ({len(jobs)}) ===")
        parts.extend(_format_job(j) for j in jobs)
        parts.append("")
    return "\n\n".join(parts).strip() + "\n"


def send_digest(new_jobs: list[dict]) -> None:
    if not new_jobs:
        log.info("no new jobs; skipping email")
        return

    try:
        user = os.environ["GMAIL_USER"]
        password = os.environ["GMAIL_APP_PASSWORD"]
    except KeyError as missing:
        log.error("missing required env var: %s; skipping email", missing)
        return
    raw_to = os.environ.get("NOTIFY_TO") or user
    recipients = [addr.strip() for addr in raw_to.split(",") if addr.strip()]
    if not recipients:
        recipients = [user]

    msg = EmailMessage()
    msg["Subject"] = f"[job-search] {len(new_jobs)} new posting(s)"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content(_format_body(new_jobs))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(user, password)
        server.send_message(msg, to_addrs=recipients)
    log.info("sent digest with %d jobs to %s", len(new_jobs), recipients)
