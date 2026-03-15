"""
src/email_delivery.py
Phase 7 — Gmail SMTP email delivery.

Builds and sends the trigger email that delivers a generated draft alert to
the recipient's inbox.

Email spec (PARAMETERS.md)
--------------------------
From:       jason.spitalnick@gmail.com  (Gmail SMTP / app password)
To:         jspitalnick@swlaw.com
Subject:    [DRAFT ALERT] {cluster description} — {Date}
Body:       - Which firms published, on what, with links
            - Date range of competitor publications
            - Confirmation S&W has not published on this topic
            - Note the .docx is a first draft for review before publication
Attachment: The generated .docx file

Implementation notes
--------------------
* Uses Python's built-in smtplib (no third-party dependency).
* Sends a multipart/alternative email (text + HTML) with the .docx attached.
* TLS via STARTTLS on port 587.
* build_alert_email() and send_alert_email() are separate so the email
  content can be unit-tested without hitting the network.
* Internally fetches cluster articles from the DB; callers only need to pass
  SaturationResult + DraftResult.
"""

import logging
import mimetypes
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from src.config import (
    EMAIL_FROM,
    EMAIL_TO,
    GMAIL_APP_PASSWORD,
    SMTP_HOST,
    SMTP_PORT,
    SW_FIRM_NAME,
)
from src import database as db
from src.cluster_detector import SaturationResult
from src.draft_generator import DraftResult

logger = logging.getLogger(__name__)


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


# ── SMTP send ──────────────────────────────────────────────────────────────────

def _build_mime_message(alert: AlertEmail) -> EmailMessage:
    """
    Build the EmailMessage object (multipart/alternative + attachment).
    Separated from the SMTP logic so it can be tested without a mail server.
    """
    msg = EmailMessage()
    msg["From"]    = alert.from_addr
    msg["To"]      = alert.to_addr
    msg["Subject"] = alert.subject

    # Set body: text first, then HTML (RFC 2046 — last part is preferred)
    msg.set_content(alert.body_text)
    msg.add_alternative(alert.body_html, subtype="html")

    # Attach the .docx
    if alert.attachment_path.exists():
        ctype, encoding = mimetypes.guess_type(str(alert.attachment_path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(alert.attachment_path, "rb") as f:
            attachment_data = f.read()
        msg.add_attachment(
            attachment_data,
            maintype=maintype,
            subtype=subtype,
            filename=alert.attachment_name,
        )
        logger.debug(
            "Attached %s (%d bytes)",
            alert.attachment_name,
            len(attachment_data),
        )
    else:
        logger.warning(
            "Attachment not found: %s — sending without attachment",
            alert.attachment_path,
        )

    return msg


def _sanitise_app_password(raw: str) -> str:
    """
    Strip whitespace (including non-breaking spaces U+00A0 inserted when
    copy-pasting from the Google Account page) and any non-alphanumeric
    characters from a Gmail app password.  The valid form is 16 lowercase
    letters, optionally grouped with spaces for readability.
    """
    return "".join(c for c in raw if c.isalnum())


def send_alert_email(alert: AlertEmail) -> bool:
    """
    Send the alert email via Gmail SMTP (STARTTLS on port 587).

    Returns True on success, False on any failure.
    Logs detailed error information on failure.
    """
    if not GMAIL_APP_PASSWORD:
        logger.error(
            "GMAIL_APP_PASSWORD is not set — cannot send email"
        )
        return False

    password = _sanitise_app_password(GMAIL_APP_PASSWORD)
    msg = _build_mime_message(alert)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(alert.from_addr, password)
            smtp.send_message(msg)

        logger.info(
            "Alert email sent: subject=%r  to=%s  attachment=%s",
            alert.subject, alert.to_addr, alert.attachment_name,
        )
        return True

    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "SMTP authentication failed (check GMAIL_APP_PASSWORD): %s", exc
        )
    except smtplib.SMTPException as exc:
        logger.error("SMTP error sending alert email: %s", exc)
    except OSError as exc:
        logger.error("Network error reaching SMTP server: %s", exc)

    return False


# ── SMTP credential check (no email sent) ─────────────────────────────────────

def check_smtp_credentials() -> bool:
    """
    Verify SMTP credentials by connecting and authenticating only.
    Does NOT send any email.  Useful for startup validation.

    Returns True if credentials are valid, False otherwise.
    """
    if not GMAIL_APP_PASSWORD:
        logger.error("GMAIL_APP_PASSWORD not set")
        return False

    password = _sanitise_app_password(GMAIL_APP_PASSWORD)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(EMAIL_FROM, password)
        logger.info("SMTP credentials verified successfully")
        return True

    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed: %s", exc)
    except smtplib.SMTPException as exc:
        logger.error("SMTP error during credential check: %s", exc)
    except OSError as exc:
        logger.error("Network error reaching SMTP server: %s", exc)

    return False
