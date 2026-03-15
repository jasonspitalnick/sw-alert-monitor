"""
tests/test_phase8.py
Phase 8 tests: Scheduler and main loop.

Structure
---------
Group 1 — ScanSummary  (dataclass correctness and log output)
Group 2 — _get_mt_time (Mountain Time helper)
Group 3 — run_scan pipeline (fully mocked — no live API, SMTP, or DB writes)
Group 4 — Scheduling logic (slot detection, deduplication, DST-safe)
Group 5 — _startup_checks (env, DB, SMTP validation paths)

Run:
    venv/bin/python tests/test_phase8.py

All groups run without network access, API keys, or Gmail credentials.
"""

import os
import sys
import tempfile
import shutil
import logging
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Redirect data files to temp dir before importing src modules ───────────────
import src.config as _cfg
_TMPDIR = tempfile.mkdtemp(prefix="alert_phase8_test_")
_cfg.DB_FILE             = Path(_TMPDIR) / "test.db"
_cfg.ALERT_TRACKER_FILE  = Path(_TMPDIR) / "alert_tracker.json"
_cfg.DRAFTED_TOPICS_FILE = Path(_TMPDIR) / "drafted_topics.json"
_cfg.DATA_DIR            = Path(_TMPDIR)
_cfg.DRAFTS_DIR          = Path(_TMPDIR) / "drafts"
_cfg.LOGS_DIR            = Path(_TMPDIR) / "logs"
_cfg.DRAFTS_DIR.mkdir(exist_ok=True)
_cfg.LOGS_DIR.mkdir(exist_ok=True)

from src import database as db
from src.cluster_detector import SaturationResult
from src.draft_generator import DraftResult
from src.gap_validator import GapValidationResult, SWArticle
from src.scheduler import (
    ScanSummary,
    _get_mt_time,
    _startup_checks,
    run_scan,
)

_MT = ZoneInfo("America/Denver")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_saturation(
    cluster_id: int = 1,
    subject_key: str = "test_sec_enforcement_2026",
    description: str = "Test: SEC enforcement procedural change 2026",
) -> SaturationResult:
    return SaturationResult(
        cluster_id=cluster_id,
        subject_key=subject_key,
        description=description,
        effective_firm_count=3,
        contributing_firms=["Gibson Dunn", "Latham & Watkins", "Kirkland & Ellis"],
        effective_firms=["Gibson Dunn", "Latham & Watkins", "Kirkland & Ellis"],
        earliest_pub_date="2026-03-10",
        latest_pub_date="2026-03-14",
        window_days=4,
    )


def _make_draft_result(subject_key: str = "test_sec_enforcement_2026") -> DraftResult:
    docx_path = _cfg.DRAFTS_DIR / f"{subject_key}_test.docx"
    docx_path.write_bytes(b"PK\x03\x04")  # minimal .docx header
    return DraftResult(
        cluster_id=1,
        subject_key=subject_key,
        docx_path=docx_path,
        draft_text="This is a test draft.",
        word_count=5,
    )


def _make_gap_has_gap(subject_key: str = "test_sec_enforcement_2026") -> GapValidationResult:
    return GapValidationResult(
        subject_key=subject_key,
        has_gap=True,
        sw_articles_checked=50,
        blocking_article=None,
        reasoning="No dedicated S&W alert found.",
    )


def _make_gap_no_gap(subject_key: str = "test_sec_enforcement_2026") -> GapValidationResult:
    return GapValidationResult(
        subject_key=subject_key,
        has_gap=False,
        sw_articles_checked=50,
        blocking_article=SWArticle(
            title="SEC Enforcement Update",
            url="https://www.swlaw.com/publications/sec-enforcement-update",
        ),
        reasoning="S&W published a dedicated alert on this topic.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Group 1 — ScanSummary
# ═══════════════════════════════════════════════════════════════════════════════

class TestScanSummary(unittest.TestCase):
    """Group 1: ScanSummary dataclass and log output."""

    def test_default_values(self):
        s = ScanSummary()
        self.assertEqual(s.scrape_total, 0)
        self.assertEqual(s.scrape_errors, 0)
        self.assertEqual(s.articles_new, 0)
        self.assertEqual(s.articles_classified, 0)
        self.assertEqual(s.clusters_saturated, 0)
        self.assertEqual(s.drafts_triggered, 0)
        self.assertEqual(s.drafts_skipped_locked, 0)
        self.assertEqual(s.drafts_skipped_no_gap, 0)
        self.assertEqual(s.drafts_failed, 0)
        self.assertEqual(s.emails_sent, 0)
        self.assertEqual(s.emails_failed, 0)
        self.assertEqual(s.triggered_subjects, [])

    def test_mutable_list_is_independent(self):
        """triggered_subjects must not share state between instances."""
        s1 = ScanSummary()
        s2 = ScanSummary()
        s1.triggered_subjects.append("topic_a")
        self.assertEqual(s2.triggered_subjects, [])

    def test_log_does_not_raise(self):
        """ScanSummary.log() should not raise."""
        s = ScanSummary(
            started_at="2026-03-14T07:00:00Z",
            finished_at="2026-03-14T07:12:34Z",
            scrape_total=120,
            scrape_errors=2,
            articles_new=47,
            articles_classified=47,
            clusters_saturated=2,
            drafts_triggered=1,
            drafts_skipped_locked=0,
            drafts_skipped_no_gap=1,
            drafts_failed=0,
            emails_sent=1,
            emails_failed=0,
            triggered_subjects=["sec_enforcement_manual_2026"],
        )
        s.log()  # must not raise

    def test_log_empty_summary_does_not_raise(self):
        ScanSummary().log()


# ═══════════════════════════════════════════════════════════════════════════════
# Group 2 — _get_mt_time
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetMtTime(unittest.TestCase):
    """Group 2: Mountain Time helper."""

    def test_returns_tuple(self):
        result = _get_mt_time()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_date_is_date_object(self):
        d, _ = _get_mt_time()
        self.assertIsInstance(d, date)

    def test_hhmm_format(self):
        """HH:MM must match the expected regex."""
        import re
        _, hhmm = _get_mt_time()
        self.assertRegex(hhmm, r"^\d{2}:\d{2}$")

    def test_time_is_mt_aware(self):
        """The returned HH:MM must correspond to Mountain Time, not UTC."""
        now_utc = datetime.now(timezone.utc)
        now_mt  = datetime.now(_MT)
        _, hhmm = _get_mt_time()
        expected_hhmm = now_mt.strftime("%H:%M")
        # Tolerate ±1 minute difference due to execution time
        self.assertEqual(hhmm, expected_hhmm)


# ═══════════════════════════════════════════════════════════════════════════════
# Group 3 — run_scan pipeline (fully mocked)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunScan(unittest.TestCase):
    """Group 3: run_scan() pipeline with all external calls mocked."""

    # ── helpers ──────────────────────────────────────────────────────────────

    def _mock_scrape_result(self, n_firms: int = 5, new_count: int = 10):
        results = []
        for i in range(n_firms):
            r = MagicMock()
            r.error = None
            r.new_count = new_count // n_firms
            results.append(r)
        return results

    # ── empty scan (nothing scraped, nothing to classify) ─────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    def test_empty_scan_returns_summary(self, mock_sat, mock_classify, mock_scrape):
        mock_scrape.return_value = self._mock_scrape_result(n_firms=3, new_count=0)
        mock_classify.return_value = []
        mock_sat.return_value = []

        summary = run_scan()

        self.assertEqual(summary.scrape_total, 3)
        self.assertEqual(summary.articles_new, 0)
        self.assertEqual(summary.clusters_saturated, 0)
        self.assertEqual(summary.drafts_triggered, 0)
        self.assertNotEqual(summary.started_at, "")
        self.assertNotEqual(summary.finished_at, "")

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    def test_scrape_error_recorded(self, mock_sat, mock_classify, mock_scrape):
        results = self._mock_scrape_result(n_firms=4, new_count=0)
        results[0].error = "Timeout"
        mock_scrape.return_value = results
        mock_classify.return_value = []
        mock_sat.return_value = []

        summary = run_scan()
        self.assertEqual(summary.scrape_errors, 1)

    # ── cluster already locked ────────────────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    def test_locked_cluster_skipped(self, mock_sm, mock_sat, mock_classify, mock_scrape):
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        saturation = _make_saturation()
        mock_sat.return_value = [saturation]
        mock_sm.is_topic_locked.return_value = True

        # fetch_sw_articles needs to be patched too
        with patch("src.scheduler.fetch_sw_articles", return_value=[]):
            summary = run_scan()

        self.assertEqual(summary.clusters_saturated, 1)
        self.assertEqual(summary.drafts_skipped_locked, 1)
        self.assertEqual(summary.drafts_triggered, 0)

    # ── gap blocked: S&W already published ────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    def test_no_gap_cluster_skipped(
        self, mock_fetch_sw, mock_gap, mock_sm, mock_sat, mock_classify, mock_scrape,
    ):
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        saturation = _make_saturation()
        mock_sat.return_value = [saturation]
        mock_sm.is_topic_locked.return_value = False
        mock_fetch_sw.return_value = []
        mock_gap.return_value = _make_gap_no_gap()

        summary = run_scan()

        self.assertEqual(summary.drafts_skipped_no_gap, 1)
        self.assertEqual(summary.drafts_triggered, 0)

    # ── happy path: full draft + email ────────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    @patch("src.scheduler.db")
    @patch("src.scheduler.build_alert_email")
    @patch("src.scheduler.send_alert_email")
    def test_happy_path_draft_and_email(
        self,
        mock_send, mock_build_email, mock_db, mock_generate,
        mock_fetch_sw, mock_gap, mock_sm, mock_sat, mock_classify, mock_scrape,
    ):
        mock_scrape.return_value = self._mock_scrape_result(n_firms=5, new_count=15)
        mock_classify.return_value = [MagicMock(is_in_scope=True)] * 3
        saturation = _make_saturation()
        mock_sat.return_value = [saturation]
        mock_sm.is_topic_locked.return_value = False
        mock_fetch_sw.return_value = [
            SWArticle(title="Other article", url="https://swlaw.com/other")
        ]
        mock_gap.return_value = _make_gap_has_gap()
        draft_result = _make_draft_result()
        mock_generate.return_value = draft_result
        mock_db.lock_cluster = MagicMock()
        mock_sm.lock_topic = MagicMock()
        mock_build_email.return_value = MagicMock()
        mock_send.return_value = True

        summary = run_scan()

        self.assertEqual(summary.clusters_saturated, 1)
        self.assertEqual(summary.drafts_triggered, 1)
        self.assertEqual(summary.emails_sent, 1)
        self.assertEqual(summary.emails_failed, 0)
        self.assertEqual(summary.drafts_skipped_locked, 0)
        self.assertEqual(summary.drafts_skipped_no_gap, 0)
        self.assertIn("test_sec_enforcement_2026", summary.triggered_subjects)

        # Verify lock was called before email
        mock_db.lock_cluster.assert_called_once()
        mock_sm.lock_topic.assert_called_once()
        mock_send.assert_called_once()

    # ── email fails: still counts as draft triggered ──────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    @patch("src.scheduler.db")
    @patch("src.scheduler.build_alert_email")
    @patch("src.scheduler.send_alert_email")
    def test_email_failure_counted(
        self,
        mock_send, mock_build_email, mock_db, mock_generate,
        mock_fetch_sw, mock_gap, mock_sm, mock_sat, mock_classify, mock_scrape,
    ):
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        saturation = _make_saturation()
        mock_sat.return_value = [saturation]
        mock_sm.is_topic_locked.return_value = False
        mock_fetch_sw.return_value = []
        mock_gap.return_value = _make_gap_has_gap()
        mock_generate.return_value = _make_draft_result()
        mock_db.lock_cluster = MagicMock()
        mock_sm.lock_topic = MagicMock()
        mock_build_email.return_value = MagicMock()
        mock_send.return_value = False  # email fails

        summary = run_scan()

        self.assertEqual(summary.drafts_triggered, 1)
        self.assertEqual(summary.emails_sent, 0)
        self.assertEqual(summary.emails_failed, 1)

    # ── generate_draft returns None ────────────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    def test_generate_draft_returns_none(
        self, mock_generate, mock_fetch_sw, mock_gap, mock_sm,
        mock_sat, mock_classify, mock_scrape,
    ):
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        mock_sat.return_value = [_make_saturation()]
        mock_sm.is_topic_locked.return_value = False
        mock_fetch_sw.return_value = []
        mock_gap.return_value = _make_gap_has_gap()
        mock_generate.return_value = None

        summary = run_scan()

        self.assertEqual(summary.drafts_failed, 1)
        self.assertEqual(summary.drafts_triggered, 0)

    # ── generate_draft raises exception ───────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    def test_generate_draft_raises(
        self, mock_generate, mock_fetch_sw, mock_gap, mock_sm,
        mock_sat, mock_classify, mock_scrape,
    ):
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        mock_sat.return_value = [_make_saturation()]
        mock_sm.is_topic_locked.return_value = False
        mock_fetch_sw.return_value = []
        mock_gap.return_value = _make_gap_has_gap()
        mock_generate.side_effect = RuntimeError("API quota exceeded")

        summary = run_scan()

        self.assertEqual(summary.drafts_failed, 1)
        self.assertEqual(summary.drafts_triggered, 0)

    # ── multiple clusters in one scan ─────────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    @patch("src.scheduler.db")
    @patch("src.scheduler.build_alert_email")
    @patch("src.scheduler.send_alert_email")
    def test_multiple_clusters(
        self,
        mock_send, mock_build_email, mock_db, mock_generate,
        mock_fetch_sw, mock_gap, mock_sm, mock_sat, mock_classify, mock_scrape,
    ):
        """Two saturated clusters: one drafts, one is already locked."""
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        sat_a = _make_saturation(cluster_id=1, subject_key="topic_a_2026")
        sat_b = _make_saturation(cluster_id=2, subject_key="topic_b_2026")
        mock_sat.return_value = [sat_a, sat_b]

        # topic_b is already locked
        mock_sm.is_topic_locked.side_effect = lambda key: key == "topic_b_2026"
        mock_fetch_sw.return_value = []
        mock_gap.return_value = _make_gap_has_gap()
        mock_generate.return_value = _make_draft_result(subject_key="topic_a_2026")
        mock_db.lock_cluster = MagicMock()
        mock_sm.lock_topic = MagicMock()
        mock_build_email.return_value = MagicMock()
        mock_send.return_value = True

        summary = run_scan()

        self.assertEqual(summary.clusters_saturated, 2)
        self.assertEqual(summary.drafts_triggered, 1)
        self.assertEqual(summary.drafts_skipped_locked, 1)
        self.assertEqual(summary.emails_sent, 1)

    # ── scrape step raises: scan continues ────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    def test_scrape_exception_does_not_crash_scan(self, mock_sat, mock_classify, mock_scrape):
        mock_scrape.side_effect = RuntimeError("Playwright crashed")
        mock_classify.return_value = []
        mock_sat.return_value = []

        # Should not raise
        summary = run_scan()
        self.assertEqual(summary.scrape_total, 0)

    # ── gap check exception defaults to has_gap=True ──────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    @patch("src.scheduler.db")
    @patch("src.scheduler.build_alert_email")
    @patch("src.scheduler.send_alert_email")
    def test_gap_check_exception_defaults_to_draft(
        self,
        mock_send, mock_build_email, mock_db, mock_generate,
        mock_fetch_sw, mock_gap, mock_sm, mock_sat, mock_classify, mock_scrape,
    ):
        """If gap check raises, the scan should still proceed to draft."""
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        mock_sat.return_value = [_make_saturation()]
        mock_sm.is_topic_locked.return_value = False
        mock_fetch_sw.return_value = []
        mock_gap.side_effect = RuntimeError("Claude API timeout")
        mock_generate.return_value = _make_draft_result()
        mock_db.lock_cluster = MagicMock()
        mock_sm.lock_topic = MagicMock()
        mock_build_email.return_value = MagicMock()
        mock_send.return_value = True

        summary = run_scan()

        # Should still trigger draft (gap defaulted to True on exception)
        self.assertEqual(summary.drafts_triggered, 1)
        self.assertEqual(summary.emails_sent, 1)

    # ── sw_articles fetched once, reused across clusters ──────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    @patch("src.scheduler.db")
    @patch("src.scheduler.build_alert_email")
    @patch("src.scheduler.send_alert_email")
    def test_sw_articles_fetched_once_for_multiple_clusters(
        self,
        mock_send, mock_build_email, mock_db, mock_generate,
        mock_fetch_sw, mock_gap, mock_sm, mock_sat, mock_classify, mock_scrape,
    ):
        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        sat_a = _make_saturation(cluster_id=1, subject_key="cluster_a_2026")
        sat_b = _make_saturation(cluster_id=2, subject_key="cluster_b_2026")
        mock_sat.return_value = [sat_a, sat_b]
        mock_sm.is_topic_locked.return_value = False
        sw_list = [SWArticle(title="Some other article", url="https://swlaw.com/a")]
        mock_fetch_sw.return_value = sw_list
        mock_gap.return_value = _make_gap_has_gap()
        mock_generate.return_value = _make_draft_result()
        mock_db.lock_cluster = MagicMock()
        mock_sm.lock_topic = MagicMock()
        mock_build_email.return_value = MagicMock()
        mock_send.return_value = True

        run_scan()

        # fetch_sw_articles must be called exactly once regardless of cluster count
        mock_fetch_sw.assert_called_once()

    # ── lock is called before email ────────────────────────────────────────

    @patch("src.scheduler.run_scrape")
    @patch("src.scheduler.classify_new_articles")
    @patch("src.scheduler.find_saturated_clusters")
    @patch("src.scheduler.sm")
    @patch("src.scheduler.validate_gap")
    @patch("src.scheduler.fetch_sw_articles")
    @patch("src.scheduler.generate_draft")
    @patch("src.scheduler.db")
    @patch("src.scheduler.build_alert_email")
    @patch("src.scheduler.send_alert_email")
    def test_lock_called_before_email(
        self,
        mock_send, mock_build_email, mock_db, mock_generate,
        mock_fetch_sw, mock_gap, mock_sm, mock_sat, mock_classify, mock_scrape,
    ):
        """Lock must be acquired before the email is sent (crash safety)."""
        call_order = []
        mock_db.lock_cluster = MagicMock(side_effect=lambda **kw: call_order.append("lock"))
        mock_sm.lock_topic = MagicMock(side_effect=lambda **kw: call_order.append("lock_topic"))
        mock_send.side_effect = lambda e: call_order.append("email") or True

        mock_scrape.return_value = self._mock_scrape_result()
        mock_classify.return_value = []
        mock_sat.return_value = [_make_saturation()]
        mock_sm.is_topic_locked.return_value = False
        mock_fetch_sw.return_value = []
        mock_gap.return_value = _make_gap_has_gap()
        mock_generate.return_value = _make_draft_result()
        mock_build_email.return_value = MagicMock()

        run_scan()

        # "lock" must appear before "email"
        self.assertIn("lock", call_order)
        self.assertIn("email", call_order)
        lock_idx  = call_order.index("lock")
        email_idx = call_order.index("email")
        self.assertLess(lock_idx, email_idx, "Lock must be acquired before email is sent")


# ═══════════════════════════════════════════════════════════════════════════════
# Group 4 — Scheduling logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchedulingLogic(unittest.TestCase):
    """Group 4: Timezone-aware slot detection and deduplication."""

    def test_slot_fires_at_correct_mt_time(self):
        """
        Simulate the scheduler loop for two tick cycles:
        first tick matches the scheduled time → scan fires.
        second tick with same time → scan does NOT fire again.
        """
        from src.scheduler import run_scheduler, SCHEDULE_TIMES_MT
        from src.config import SCHEDULE_TIMES_MT as schedule_times

        target_time = schedule_times[0]  # "07:00"
        target_date = date(2026, 3, 16)  # Monday — weekday

        tick_count = 0

        def fake_get_mt_time():
            nonlocal tick_count
            tick_count += 1
            # First two ticks: matching time; third tick: next minute (loop exits)
            if tick_count <= 2:
                return target_date, target_time
            return target_date, "99:99"  # sentinal; loop won't exit so we raise

        scan_call_count = [0]

        def fake_run_scan():
            scan_call_count[0] += 1
            return ScanSummary()

        def fake_sleep(seconds):
            if tick_count >= 3:
                raise KeyboardInterrupt("stop test")

        with patch("src.scheduler._get_mt_time", side_effect=fake_get_mt_time), \
             patch("src.scheduler.run_scan", side_effect=fake_run_scan), \
             patch("src.scheduler.time.sleep", side_effect=fake_sleep), \
             patch("src.scheduler.SCHEDULE_TIMES_MT", [target_time]), \
             patch("src.scheduler.SCHEDULE_WEEKDAYS_ONLY", False):
            try:
                from src.scheduler import run_scheduler
                run_scheduler()
            except KeyboardInterrupt:
                pass

        # scan should have fired exactly once despite two matching ticks
        self.assertEqual(scan_call_count[0], 1)

    def test_slot_fires_on_different_days(self):
        """
        Same HH:MM on two different calendar days → scan fires on BOTH days.
        """
        from src.config import SCHEDULE_TIMES_MT as schedule_times

        target_time = schedule_times[0]
        day1 = date(2026, 3, 16)  # Monday
        day2 = date(2026, 3, 17)  # Tuesday

        tick_sequence = [
            (day1, target_time),
            (day1, "23:59"),  # day 1 non-matching
            (day2, target_time),
            (day2, "99:99"),   # sentinel: stop
        ]
        tick_idx = [0]

        def fake_get_mt_time():
            result = tick_sequence[min(tick_idx[0], len(tick_sequence) - 1)]
            tick_idx[0] += 1
            return result

        scan_call_count = [0]

        def fake_run_scan():
            scan_call_count[0] += 1
            return ScanSummary()

        sleep_count = [0]

        def fake_sleep(seconds):
            sleep_count[0] += 1
            if tick_idx[0] >= len(tick_sequence):
                raise KeyboardInterrupt("stop test")

        with patch("src.scheduler._get_mt_time", side_effect=fake_get_mt_time), \
             patch("src.scheduler.run_scan", side_effect=fake_run_scan), \
             patch("src.scheduler.time.sleep", side_effect=fake_sleep), \
             patch("src.scheduler.SCHEDULE_TIMES_MT", [target_time]), \
             patch("src.scheduler.SCHEDULE_WEEKDAYS_ONLY", False):
            try:
                from src.scheduler import run_scheduler
                run_scheduler()
            except KeyboardInterrupt:
                pass

        self.assertEqual(scan_call_count[0], 2, "Scan should fire once on each calendar day")

    def test_schedule_times_mt_has_expected_values(self):
        """Config must define exactly one schedule time, weekdays only."""
        self.assertEqual(len(_cfg.SCHEDULE_TIMES_MT), 1)
        for t in _cfg.SCHEDULE_TIMES_MT:
            self.assertRegex(t, r"^\d{2}:\d{2}$")
        self.assertTrue(_cfg.SCHEDULE_WEEKDAYS_ONLY)

    def test_weekend_scan_skipped(self):
        """Scan must NOT fire on Saturday or Sunday when SCHEDULE_WEEKDAYS_ONLY=True."""
        target_time = _cfg.SCHEDULE_TIMES_MT[0]
        saturday = date(2026, 3, 14)   # Saturday
        sunday   = date(2026, 3, 15)   # Sunday

        tick_sequence = [
            (saturday, target_time),
            (sunday,   target_time),
            (sunday,   "99:99"),       # sentinel: stop
        ]
        tick_idx = [0]

        def fake_get_mt_time():
            result = tick_sequence[min(tick_idx[0], len(tick_sequence) - 1)]
            tick_idx[0] += 1
            return result

        scan_call_count = [0]

        def fake_run_scan():
            scan_call_count[0] += 1
            return ScanSummary()

        def fake_sleep(seconds):
            if tick_idx[0] >= len(tick_sequence):
                raise KeyboardInterrupt("stop test")

        with patch("src.scheduler._get_mt_time", side_effect=fake_get_mt_time), \
             patch("src.scheduler.run_scan", side_effect=fake_run_scan), \
             patch("src.scheduler.time.sleep", side_effect=fake_sleep), \
             patch("src.scheduler.SCHEDULE_TIMES_MT", [target_time]), \
             patch("src.scheduler.SCHEDULE_WEEKDAYS_ONLY", True):
            try:
                from src.scheduler import run_scheduler
                run_scheduler()
            except KeyboardInterrupt:
                pass

        self.assertEqual(scan_call_count[0], 0, "Scan must not fire on weekends")


# ═══════════════════════════════════════════════════════════════════════════════
# Group 5 — _startup_checks
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartupChecks(unittest.TestCase):
    """Group 5: startup validation paths."""

    def setUp(self):
        db.init_db()  # ensure DB is ready for tests

    @patch("src.scheduler.validate_env")
    @patch("src.scheduler.db")
    @patch("src.scheduler.check_smtp_credentials")
    def test_all_passing(self, mock_smtp, mock_db, mock_env):
        mock_env.return_value = []
        mock_db.init_db = MagicMock()
        mock_smtp.return_value = True

        result = _startup_checks(skip_smtp=False)
        self.assertTrue(result)

    @patch("src.scheduler.validate_env")
    def test_missing_env_returns_false(self, mock_env):
        mock_env.return_value = ["ANTHROPIC_API_KEY"]
        result = _startup_checks(skip_smtp=True)
        self.assertFalse(result)

    @patch("src.scheduler.validate_env")
    @patch("src.scheduler.db")
    def test_db_init_failure_returns_false(self, mock_db, mock_env):
        mock_env.return_value = []
        mock_db.init_db.side_effect = RuntimeError("DB locked")
        result = _startup_checks(skip_smtp=True)
        self.assertFalse(result)

    @patch("src.scheduler.validate_env")
    @patch("src.scheduler.db")
    @patch("src.scheduler.check_smtp_credentials")
    def test_smtp_failure_is_non_fatal(self, mock_smtp, mock_db, mock_env):
        """SMTP failure should log a warning but still return True."""
        mock_env.return_value = []
        mock_db.init_db = MagicMock()
        mock_smtp.return_value = False

        result = _startup_checks(skip_smtp=False)
        self.assertTrue(result, "SMTP failure is non-fatal — should still return True")

    @patch("src.scheduler.validate_env")
    @patch("src.scheduler.db")
    def test_skip_smtp_check(self, mock_db, mock_env):
        mock_env.return_value = []
        mock_db.init_db = MagicMock()

        with patch("src.scheduler.check_smtp_credentials") as mock_smtp:
            _startup_checks(skip_smtp=True)
            mock_smtp.assert_not_called()


# ── Test runner ────────────────────────────────────────────────────────────────

def run_tests():
    groups = [
        ("Group 1 — ScanSummary",      TestScanSummary),
        ("Group 2 — _get_mt_time",     TestGetMtTime),
        ("Group 3 — run_scan pipeline", TestRunScan),
        ("Group 4 — Scheduling logic", TestSchedulingLogic),
        ("Group 5 — _startup_checks",  TestStartupChecks),
    ]

    total_passed = 0
    total_failed = 0

    for group_name, test_class in groups:
        print(f"\n{'─' * 60}")
        print(f"  {group_name}")
        print(f"{'─' * 60}")

        loader = unittest.TestLoader()
        suite  = loader.loadTestsFromTestCase(test_class)
        runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
        result = runner.run(suite)

        passed = result.testsRun - len(result.failures) - len(result.errors)
        total_passed += passed
        total_failed += len(result.failures) + len(result.errors)

    print(f"\n{'═' * 60}")
    print(f"  TOTAL:  {total_passed} passed  |  {total_failed} failed")
    print(f"{'═' * 60}")

    # Cleanup temp dir
    shutil.rmtree(_TMPDIR, ignore_errors=True)

    return total_failed


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)  # silence logs during tests
    failures = run_tests()
    sys.exit(0 if failures == 0 else 1)
