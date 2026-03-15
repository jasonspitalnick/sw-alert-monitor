"""
src/email_delivery.py
Phase 7 — Email delivery via SendGrid HTTP API.

Builds and sends the trigger email that delivers a generated draft alert to
the recipient's inbox.

Email spec (PARAMETERS.md)
--------------------------
From:       jason.spitalnick@gmail.com  (SendGrid verified sender)
To:         jspitalnick@swlaw.com
Subject:    [DRAFT ALERT] {cluster description} — {Date}
Body:       - Which firms published, on what, with links
            - Date range of competitor publications
            - Confirmation S&W has not published on this topic
            - Note the .docx is a first draft for review before publication
Attachment: The generated .docx file

Implementation notes
--------------------
* Uses the SendGrid Web API v3 via httpx (already a project dependency).
* Plain SMTP is intentionally avoided: Railway runs on Google Cloud Platform,
  which blocks outbound connections on ports 25, 465, and 587 at the
  infrastructure level.  HTTPS (port 443) is always open.
* build_alert_email() and send_alert_email() are separate so the email
  content can be unit-tested without hitting the network.
* check_smtp_credentials() is kept for API compatibility with scheduler.py
  but now validates the SendGrid API key instead of opening a socket.
"""

import base64
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from src.config import (
    EMAIL_FROM,
    EMAIL_TO,
    SENDGRID_API_KEY,
    SW_FIRM_NAME,
)
from src import database as db
from src.cluster_detector import SaturationResult
from src.draft_generator import DraftResult

logger = logging.getLogger(__name__)

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class AlertEmail:
    """All fields needed to send the trigger email."""
    to_addr: str
    from_addr: str
    subject: str
    body_text: str           # plain-text fallback
    body_html: str           # primary HTML body
    attachment_path: Path
    attachment_name: str     # filename shown to the recipient


# ── Subject line ───────────────────────────────────────────────────────────────

def build_subject(saturation: SaturationResult) -> str:
    """
    Build the email subject line.
    Format: [DRAFT ALERT] {cluster description} — {Date}
    """
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    desc = saturation.description
    # Keep subject line reasonably compact
    if len(desc) > 90:
        desc = desc[:87].rstrip() + "..."
    return f"[DRAFT ALERT] {desc} — {today}"


# ── Body builders ──────────────────────────────────────────────────────────────

def _format_articles_text(articles) -> str:
    """Format the competitor article list as plain text."""
    lines = []
    for art in articles:
        date_str = art["date_published"] or "n/a"
        lines.append(f"  • {art['firm_name']} ({date_str})")
        lines.append(f"    {art['title']}")
        lines.append(f"    {art['url']}")
    return "\n".join(lines)


def _format_articles_html(articles) -> str:
    """Format the competitor article list as HTML table rows."""
    rows = []
    for art in articles:
        date_str = art["date_published"] or "n/a"
        safe_title = art["title"].replace("<", "&lt;").replace(">", "&gt;")
        safe_url   = art["url"].replace('"', "%22")
        safe_firm  = art["firm_name"].replace("<", "&lt;")
        rows.append(
            f'<tr>'
            f'<td style="padding:4px 8px;vertical-align:top"><strong>{safe_firm}</strong></td>'
            f'<td style="padding:4px 8px;vertical-align:top">{date_str}</td>'
            f'<td style="padding:4px 8px;vertical-align:top">'
            f'<a href="{safe_url}">{safe_title}</a></td>'
            f'</tr>'
        )
    return "\n".join(rows)


def build_body_text(
    saturation: SaturationResult,
    draft_result: DraftResult,
    articles,
) -> str:
    """Build the plain-text email body."""
    firms_str = ", ".join(saturation.effective_firms)
    date_range = (
        f"{saturation.earliest_pub_date} to {saturation.latest_pub_date} "
        f"({saturation.window_days} days)"
    )
    articles_block = _format_articles_text(articles)

    return f"""\
Alert Monitor has detected a topic cluster that meets the drafting threshold.
A draft client alert has been generated and is attached to this email.

─────────────────────────────────────────────────
TRIGGERING SUBJECT
─────────────────────────────────────────────────
{saturation.description}

─────────────────────────────────────────────────
COMPETITOR COVERAGE
─────────────────────────────────────────────────
Firms:          {firms_str}
Date range:     {date_range}
Articles found: {len(articles)}

{articles_block}

─────────────────────────────────────────────────
GAP STATUS
─────────────────────────────────────────────────
{SW_FIRM_NAME} has not published a dedicated alert on this specific
narrow subject. Gap condition met — draft triggered.

─────────────────────────────────────────────────
ATTACHED DRAFT
─────────────────────────────────────────────────
File: {draft_result.docx_path.name}
Approximate word count: {draft_result.word_count}

The attached document is a first draft generated by the Alert Monitor.
Please review and edit it thoroughly before publication.

—
Alert Monitor  |  {SW_FIRM_NAME}
"""


def build_body_html(
    saturation: SaturationResult,
    draft_result: DraftResult,
    articles,
) -> str:
    """Build the HTML email body."""
    firms_str  = ", ".join(saturation.effective_firms)
    date_range = (
        f"{saturation.earliest_pub_date} to {saturation.latest_pub_date} "
        f"({saturation.window_days} days)"
    )
    article_rows = _format_articles_html(articles)
    safe_desc    = saturation.description.replace("<", "&lt;").replace(">", "&gt;")
    safe_fname   = draft_result.docx_path.name.replace("<", "&lt;")

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:700px;margin:0 auto;">

<p style="font-size:16px;font-weight:bold;color:#1a3a5c;">
  ⚖️ Alert Monitor — Draft Ready for Review
</p>

<hr style="border:none;border-top:1px solid #ccc;margin:12px 0;">

<h3 style="margin:0 0 4px 0;color:#1a3a5c;">Triggering Subject</h3>
<p style="margin:0 0 16px 0;">{safe_desc}</p>

<h3 style="margin:0 0 8px 0;color:#1a3a5c;">Competitor Coverage</h3>
<table style="border-collapse:collapse;width:100%;margin-bottom:16px;">
  <thead>
    <tr style="background:#f0f4f8;">
      <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ccc;">Firm</th>
      <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ccc;">Published</th>
      <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ccc;">Article</th>
    </tr>
  </thead>
  <tbody>
{article_rows}
  </tbody>
</table>

<p style="margin:0 0 4px 0;">
  <strong>Date range:</strong> {date_range}
  &nbsp;&nbsp;|&nbsp;&nbsp;
  <strong>Firms covered:</strong> {firms_str}
</p>

<hr style="border:none;border-top:1px solid #ccc;margin:16px 0;">

<h3 style="margin:0 0 4px 0;color:#1a3a5c;">Gap Status</h3>
<p style="margin:0 0 16px 0;">
  <span style="color:#2a7a2a;">✔</span>
  &nbsp;<strong>{SW_FIRM_NAME}</strong> has not published a dedicated alert on this
  specific narrow subject. Gap condition met — draft triggered.
</p>

<hr style="border:none;border-top:1px solid #ccc;margin:16px 0;">

<h3 style="margin:0 0 4px 0;color:#1a3a5c;">Attached Draft</h3>
<p style="margin:0 0 4px 0;">
  <strong>File:</strong> {safe_fname}<br>
  <strong>Approximate word count:</strong> {draft_result.word_count}
</p>
<p style="margin:8px 0 0 0;padding:10px;background:#fffbe6;border-left:4px solid #f0a500;">
  ⚠️ This document is a <strong>first draft</strong> generated by the Alert Monitor.
  Please review and edit thoroughly before publication.
</p>

<hr style="border:none;border-top:1px solid #ccc;margin:16px 0;">
<p style="font-size:12px;color:#888;">Alert Monitor &nbsp;|&nbsp; {SW_FIRM_NAME}</p>

</body>
</html>"""


# ── Email assembly ─────────────────────────────────────────────────────────────

def build_alert_email(
    saturation: SaturationResult,
    draft_result: DraftResult,
) -> AlertEmail:
    """
    Assemble the AlertEmail for a triggered draft.

    Fetches the cluster articles from the DB, builds the subject and body,
    and prepares the .docx attachment metadata.

    Parameters
    ----------
    saturation : SaturationResult
        The cluster that triggered drafting.
    draft_result : DraftResult
        The generated draft (provides .docx path and word count).

    Returns
    -------
    AlertEmail ready to be passed to send_alert_email().
    """
    articles = db.get_cluster_articles(saturation.cluster_id)

    subject    = build_subject(saturation)
    body_text  = build_body_text(saturation, draft_result, articles)
    body_html  = build_body_html(saturation, draft_result, articles)

    return AlertEmail(
        to_addr=EMAIL_TO,
        from_addr=EMAIL_FROM,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachment_path=draft_result.docx_path,
        attachment_name=draft_result.docx_path.name,
    )


# ── SendGrid HTTP send ─────────────────────────────────────────────────────────

def send_alert_email(alert: AlertEmail) -> bool:
    """
    Send the alert email via the SendGrid Web API v3 (HTTPS, port 443).

    Railway/GCP blocks outbound SMTP (ports 25/465/587), so smtplib cannot
    be used from a hosted container.  The SendGrid HTTP API is unaffected
    by that restriction.

    Returns True on success, False on any failure.
    Logs detailed error information on failure.
    """
    if not SENDGRID_API_KEY:
        logger.error("SENDGRID_API_KEY is not set — cannot send email")
        return False

    # ── Build SendGrid payload ─────────────────────────────────────────────
    payload: dict = {
        "personalizations": [{"to": [{"email": alert.to_addr}]}],
        "from": {"email": alert.from_addr},
        "subject": alert.subject,
        "content": [
            {"type": "text/plain", "value": alert.body_text},
            {"type": "text/html",  "value": alert.body_html},
        ],
    }

    # Attach .docx if it exists
    if alert.attachment_path.exists():
        with open(alert.attachment_path, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode()
        ctype, _ = mimetypes.guess_type(str(alert.attachment_path))
        if not ctype:
            ctype = "application/octet-stream"
        payload["attachments"] = [
            {
                "content":     encoded,
                "filename":    alert.attachment_name,
                "type":        ctype,
                "disposition": "attachment",
            }
        ]
        logger.debug("Attachment encoded: %s", alert.attachment_name)
    else:
        logger.warning(
            "Attachment not found: %s — sending without attachment",
            alert.attachment_path,
        )

    # ── POST to SendGrid ───────────────────────────────────────────────────
    try:
        response = httpx.post(
            _SENDGRID_URL,
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        logger.info(
            "Alert email sent via SendGrid: subject=%r  to=%s  attachment=%s",
            alert.subject, alert.to_addr, alert.attachment_name,
        )
        return True

    except httpx.HTTPStatusError as exc:
        logger.error(
            "SendGrid API error %d: %s",
            exc.response.status_code,
            exc.response.text,
        )
    except httpx.RequestError as exc:
        logger.error("Network error reaching SendGrid API: %s", exc)

    return False


# ── Credential check (startup validation) ─────────────────────────────────────

def check_smtp_credentials() -> bool:
    """
    Validate that the SendGrid API key is configured.

    Named check_smtp_credentials() for API compatibility with scheduler.py.
    No network call is made here; the key is validated on first send.

    Returns True if the key is present, False otherwise.
    """
    if not SENDGRID_API_KEY:
        logger.error("SENDGRID_API_KEY not set — email delivery disabled")
        return False

    logger.info("SendGrid API key configured.")
    return True
