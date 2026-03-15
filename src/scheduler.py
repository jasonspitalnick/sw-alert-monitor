"""
src/scheduler.py
Phase 8 — Main scan loop and scheduler.

Orchestrates the full scan pipeline, twice daily at the times defined in
SCHEDULE_TIMES_MT (Mountain Time):

  1. Scrape all competitor firm publication pages  (scraper.run_scrape)
  2. Classify new articles                         (classifier.classify_new_articles)
  3. Detect saturated clusters                     (cluster_detector.find_saturated_clusters)
  4. For each saturated cluster:
       a. Skip if already locked
       b. Validate S&W gap                         (gap_validator.validate_gap)
          — pre-fetches S&W articles ONCE per scan, reuses across all clusters
       c. If gap exists: generate draft → lock cluster → send email
  5. Log a concise scan summary

Scheduling
----------
Uses zoneinfo (Python 3.9+) for Mountain Time scheduling so it works
correctly on UTC-hosted servers (Railway, Render) through DST transitions.
Checks every 60 seconds; fires at most once per scheduled slot per calendar day.

Lock ordering
-------------
The cluster is locked in BOTH the SQLite DB and drafted_topics.json BEFORE the
email is sent.  A crash during SMTP delivery will not cause a re-draft on the
next scan.

CLI
---
  python -m src.scheduler            # enter the scheduling loop
  python -m src.scheduler --run-once # run one scan immediately, then exit
  python -m src.scheduler --verbose  # debug-level logging
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src import database as db
from src import state_manager as sm
from src.classifier import classify_new_articles
from src.cluster_detector import find_saturated_clusters
from src.config import LOGS_DIR, SCHEDULE_TIMES_MT, SCHEDULE_WEEKDAYS_ONLY, validate_env
from src.draft_generator import generate_draft
from src.email_delivery import (
    build_alert_email,
    check_smtp_credentials,
    send_alert_email,
)
from src.gap_validator import GapValidationResult, fetch_sw_articles, validate_gap
from src.scraper import run_scrape

logger = logging.getLogger(__name__)

_MT = ZoneInfo("America/Denver")


# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> None:
    """
    Configure root logger: file handler (logs/scheduler.log) + stderr.
    Called once at startup before any other logging.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "scheduler.log"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


# ── Scan summary ───────────────────────────────────────────────────────────────

@dataclass
class ScanSummary:
    """Metrics captured during one full scan run."""
    started_at: str = ""
    finished_at: str = ""
    scrape_total: int = 0           # firms attempted
    scrape_errors: int = 0          # firms that raised exceptions
    articles_new: int = 0           # articles written to DB this run
    articles_classified: int = 0    # articles sent through classifier
    clusters_saturated: int = 0     # clusters meeting saturation threshold
    drafts_triggered: int = 0       # drafts actually generated
    drafts_skipped_locked: int = 0  # skipped — already drafted
    drafts_skipped_no_gap: int = 0  # skipped — S&W already covered it
    drafts_failed: int = 0          # draft generation raised an exception
    emails_sent: int = 0            # emails successfully delivered
    emails_failed: int = 0          # emails that failed to send
    triggered_subjects: list[str] = field(default_factory=list)

    def log(self) -> None:
        logger.info("─" * 60)
        logger.info("SCAN SUMMARY  %s → %s", self.started_at, self.finished_at)
        logger.info(
            "  Scrape:      %d firms, %d errors",
            self.scrape_total, self.scrape_errors,
        )
        logger.info(
            "  Articles:    %d new scraped, %d classified",
            self.articles_new, self.articles_classified,
        )
        logger.info("  Saturated:   %d clusters", self.clusters_saturated)
        logger.info(
            "  Drafts:      %d triggered, %d already-locked, %d no-gap, %d failed",
            self.drafts_triggered, self.drafts_skipped_locked,
            self.drafts_skipped_no_gap, self.drafts_failed,
        )
        logger.info(
            "  Emails:      %d sent, %d failed",
            self.emails_sent, self.emails_failed,
        )
        if self.triggered_subjects:
            for subj in self.triggered_subjects:
                logger.info("  ✔ %s", subj)
        logger.info("─" * 60)


# ── Core scan pipeline ─────────────────────────────────────────────────────────

def run_scan() -> ScanSummary:
    """
    Execute one full scan cycle:
      scrape → classify → detect saturation → gap check → draft → email

    Never raises; all exceptions are caught and logged at each step.
    Returns a ScanSummary with metrics from this run.
    """
    summary = ScanSummary(
        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    # ── Step 1: Scrape ─────────────────────────────────────────────────────────
    logger.info("=== Step 1/4: Scraping competitor publications ===")
    try:
        scrape_results = run_scrape()
        summary.scrape_total = len(scrape_results)
        summary.scrape_errors = sum(1 for r in scrape_results if r.error)
        summary.articles_new = sum(r.new_count for r in scrape_results)
        logger.info(
            "Scrape complete: %d firms, %d new articles, %d errors",
            summary.scrape_total, summary.articles_new, summary.scrape_errors,
        )
    except Exception as exc:
        logger.error("Scrape step failed: %s", exc, exc_info=True)

    # ── Step 2: Classify ───────────────────────────────────────────────────────
    logger.info("=== Step 2/4: Classifying new articles ===")
    try:
        classification_results = classify_new_articles(batch_limit=200)
        summary.articles_classified = len(classification_results)
        logger.info("Classified %d articles", summary.articles_classified)
    except Exception as exc:
        logger.error("Classification step failed: %s", exc, exc_info=True)

    # ── Step 3: Detect saturation ──────────────────────────────────────────────
    logger.info("=== Step 3/4: Checking cluster saturation ===")
    saturated = []
    try:
        saturated = find_saturated_clusters()
        summary.clusters_saturated = len(saturated)
        if not saturated:
            logger.info("No clusters meet saturation threshold — scan complete.")
    except Exception as exc:
        logger.error("Saturation check failed: %s", exc, exc_info=True)

    if not saturated:
        summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary.log()
        return summary

    # ── Step 4: Gap check + draft + email ─────────────────────────────────────
    logger.info(
        "=== Step 4/4: Gap validation + drafting (%d saturated cluster(s)) ===",
        len(saturated),
    )

    # Pre-fetch S&W articles ONCE; reuse across all saturated clusters this scan.
    # Avoids hammering swlaw.com on scans with multiple simultaneous triggers.
    sw_articles = []
    try:
        sw_articles = fetch_sw_articles()
        logger.info("Fetched %d S&W articles for gap checks", len(sw_articles))
    except Exception as exc:
        logger.error(
            "Failed to fetch S&W articles (%s) — gap checks will proceed without pre-fetch",
            exc,
        )
        sw_articles = []  # validate_gap handles None gracefully (fetches itself)

    for saturation in saturated:
        subject_key = saturation.subject_key
        logger.info("Evaluating cluster: %s", subject_key)

        # ── Belt-and-suspenders lock check ─────────────────────────────────
        # The DB already filters to 'active' clusters, but drafted_topics.json
        # provides an independent safety net that requires no DB query.
        if sm.is_topic_locked(subject_key):
            logger.info("  SKIP (already locked): %s", subject_key)
            summary.drafts_skipped_locked += 1
            continue

        # ── Gap validation ─────────────────────────────────────────────────
        gap_result: GapValidationResult
        try:
            gap_result = validate_gap(
                subject_key=subject_key,
                subject_description=saturation.description,
                sw_articles=sw_articles if sw_articles else None,
            )
        except Exception as exc:
            logger.error(
                "  Gap check raised exception for %s: %s — treating as gap present",
                subject_key, exc, exc_info=True,
            )
            # Safer to draft than to silently suppress on an exception
            gap_result = GapValidationResult(
                subject_key=subject_key,
                has_gap=True,
                sw_articles_checked=0,
                blocking_article=None,
                reasoning=f"Gap check failed ({exc}); defaulting to has_gap=True",
            )

        if not gap_result.has_gap:
            blocking = (
                gap_result.blocking_article.title
                if gap_result.blocking_article
                else "unknown article"
            )
            logger.info(
                "  SKIP (no gap): %s — S&W already published: %s",
                subject_key, blocking,
            )
            summary.drafts_skipped_no_gap += 1
            continue

        # ── Generate draft ─────────────────────────────────────────────────
        logger.info("  TRIGGERING DRAFT: %s", subject_key)
        try:
            draft_result = generate_draft(saturation)
        except Exception as exc:
            logger.error(
                "  Draft generation failed for %s: %s",
                subject_key, exc, exc_info=True,
            )
            summary.drafts_failed += 1
            continue

        if draft_result is None:
            logger.error("  generate_draft() returned None for %s", subject_key)
            summary.drafts_failed += 1
            continue

        logger.info(
            "  Draft ready: %s (%d words)",
            draft_result.docx_path.name, draft_result.word_count,
        )
        summary.drafts_triggered += 1
        summary.triggered_subjects.append(subject_key)

        # ── Lock BEFORE sending email ──────────────────────────────────────
        # If the SMTP send crashes, the lock prevents a re-draft on the next
        # scan.  The .docx file remains on disk for manual forwarding.
        try:
            db.lock_cluster(
                cluster_id=saturation.cluster_id,
                draft_file=str(draft_result.docx_path),
            )
            sm.lock_topic(
                subject_key=subject_key,
                description=saturation.description,
                draft_file=str(draft_result.docx_path),
                trigger_firms=saturation.effective_firms,
            )
            logger.info("  Cluster locked: %s", subject_key)
        except Exception as exc:
            logger.error(
                "  Failed to lock cluster %s: %s — continuing to email step",
                subject_key, exc, exc_info=True,
            )

        # ── Send alert email ───────────────────────────────────────────────
        try:
            alert_email = build_alert_email(saturation, draft_result)
            sent = send_alert_email(alert_email)
            if sent:
                summary.emails_sent += 1
                logger.info("  Email delivered: %s", subject_key)
            else:
                summary.emails_failed += 1
                logger.warning("  Email send failed: %s", subject_key)
        except Exception as exc:
            logger.error(
                "  Email step raised exception for %s: %s",
                subject_key, exc, exc_info=True,
            )
            summary.emails_failed += 1

    summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary.log()
    return summary


# ── Timezone-aware scheduler ───────────────────────────────────────────────────

def _get_mt_time() -> tuple[date, str]:
    """Return the current (date, HH:MM) in Mountain Time."""
    now_mt = datetime.now(_MT)
    return now_mt.date(), now_mt.strftime("%H:%M")


def run_scheduler() -> None:
    """
    Main scheduling loop.  Runs indefinitely, executing run_scan() at each
    time in SCHEDULE_TIMES_MT (Mountain Time).

    When SCHEDULE_WEEKDAYS_ONLY is True, scans are skipped on Saturday and
    Sunday (weekday() values 5 and 6).

    Checks every 60 seconds.  Each (calendar-date, HH:MM) slot fires at most
    once, so a scan that runs past the minute boundary does not re-trigger.

    Mountain Time is used directly (via zoneinfo), so DST transitions are
    handled automatically without UTC offset arithmetic.
    """
    days_note = " (weekdays only)" if SCHEDULE_WEEKDAYS_ONLY else ""
    logger.info(
        "Scheduler running.  Scheduled times (Mountain Time): %s%s",
        ", ".join(SCHEDULE_TIMES_MT), days_note,
    )

    # Track which (date, HH:MM) slots have already been executed
    executed: set[tuple[date, str]] = set()

    while True:
        current_date, current_hhmm = _get_mt_time()

        # Weekday guard: Monday=0 … Friday=4; Saturday=5, Sunday=6
        is_weekday = current_date.weekday() < 5
        if SCHEDULE_WEEKDAYS_ONLY and not is_weekday:
            # Prune stale slots and sleep without scanning
            executed = {(d, t) for d, t in executed if d == current_date}
            time.sleep(60)
            continue

        for slot_time in SCHEDULE_TIMES_MT:
            slot_key = (current_date, slot_time)
            if current_hhmm == slot_time and slot_key not in executed:
                logger.info(
                    "Scheduled scan triggered at %s Mountain Time", slot_time
                )
                executed.add(slot_key)
                try:
                    run_scan()
                except Exception as exc:
                    # run_scan() itself should never raise, but be defensive
                    logger.error(
                        "Unexpected error in run_scan(): %s",
                        exc, exc_info=True,
                    )

        # Prune slots from prior calendar days to avoid unbounded set growth
        executed = {(d, t) for d, t in executed if d == current_date}

        time.sleep(60)


# ── Startup validation ─────────────────────────────────────────────────────────

def _startup_checks(skip_smtp: bool = False) -> bool:
    """
    Validate environment and connectivity at startup.

    Returns True if all critical checks pass.  SMTP failure is non-fatal —
    it logs a warning and still returns True so the scheduler can generate
    drafts even if email delivery is temporarily broken.
    """
    ok = True

    # Environment variables
    missing = validate_env()
    if missing:
        logger.error(
            "Missing required environment variables: %s",
            ", ".join(missing),
        )
        ok = False

    # Database initialisation
    try:
        db.init_db()
        logger.info("Database initialised.")
    except Exception as exc:
        logger.error("Database init failed: %s", exc, exc_info=True)
        ok = False

    # SMTP credentials (non-fatal — drafts still generated if email is broken)
    if not skip_smtp and ok:
        logger.info("Verifying SMTP credentials...")
        if check_smtp_credentials():
            logger.info("SMTP credentials verified.")
        else:
            logger.warning(
                "SMTP credential check failed — emails will not deliver.  "
                "Drafts will still be generated and saved to drafts/.  "
                "Check GMAIL_APP_PASSWORD in .env."
            )

    return ok


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> int:
    """
    Entry point.  Parse CLI args, run startup checks, then either execute a
    single scan (--run-once) or enter the scheduling loop.

    Returns exit code: 0 = success, 1 = fatal startup failure.
    """
    parser = argparse.ArgumentParser(
        prog="alert-monitor",
        description="Snell & Wilmer Alert Monitor — competitor intelligence scanner",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help=(
            "Execute one scan immediately and exit "
            "(useful for CI / manual deployment checks)"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    parser.add_argument(
        "--skip-smtp-check",
        action="store_true",
        help="Skip SMTP credential verification at startup",
    )
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)
    logger.info("Alert Monitor starting (Phase 8)")

    if not _startup_checks(skip_smtp=args.skip_smtp_check):
        logger.error("Startup checks failed — exiting.")
        return 1

    if args.run_once:
        logger.info("--run-once: executing single scan")
        summary = run_scan()
        logger.info(
            "Single scan complete: %d draft(s) triggered, %d email(s) sent",
            summary.drafts_triggered, summary.emails_sent,
        )
        return 0

    run_scheduler()
    return 0  # unreachable in normal operation (loop exits via KeyboardInterrupt)


if __name__ == "__main__":
    sys.exit(main())
