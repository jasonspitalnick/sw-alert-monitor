"""
tests/test_phase2.py
Phase 2 tests: SQLite schema + state manager.

Run with:
    venv/bin/python -m pytest tests/test_phase2.py -v
or directly:
    venv/bin/python tests/test_phase2.py
"""

import os
import sys
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Redirect DB and JSON files to a temp directory for tests ──────────────────
_TMPDIR = tempfile.mkdtemp(prefix="alert_monitor_test_")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")

# Monkey-patch config paths before importing any app modules
import src.config as _cfg
_cfg.DB_FILE               = Path(_TMPDIR) / "test_alerts.db"
_cfg.ALERT_TRACKER_FILE    = Path(_TMPDIR) / "alert_tracker.json"
_cfg.DRAFTED_TOPICS_FILE   = Path(_TMPDIR) / "drafted_topics.json"
_cfg.DATA_DIR              = Path(_TMPDIR)
_cfg.DRAFTS_DIR            = Path(_TMPDIR) / "drafts"
_cfg.DRAFTS_DIR.mkdir(exist_ok=True)

from src import database as db
from src import state_manager as sm


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _iso(days_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(days_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%d")


PASS = "✅ PASS"
FAIL = "❌ FAIL"

_results: list[tuple[str, bool, str]] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    _results.append((label, condition, detail))
    marker = "  " + status
    if detail:
        marker += f"  [{detail}]"
    print(f"{marker}  {label}")
    if not condition:
        raise AssertionError(f"FAILED: {label}  {detail}")


# ══════════════════════════════════════════════════════════════════════════════
# Test group 1 — Database schema and init
# ══════════════════════════════════════════════════════════════════════════════

def test_db_init():
    print("\n── DB: init ──")
    db.init_db()
    # Confirm tables exist by querying sqlite_master
    with db.get_conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    expected = {"articles", "clusters", "cluster_articles", "scrape_log"}
    check("All four tables created", expected.issubset(tables), str(tables))
    check("init_db is idempotent (re-run without error)", True)
    db.init_db()   # second call should not raise


# ══════════════════════════════════════════════════════════════════════════════
# Test group 2 — Articles CRUD
# ══════════════════════════════════════════════════════════════════════════════

def test_articles():
    print("\n── DB: articles ──")

    # Insert a new article
    aid = db.upsert_article(
        firm_name="Gibson Dunn",
        title="SEC Updates Enforcement Manual",
        url="https://gibsondunn.com/sec-enforcement-manual-2026",
        date_published=_date(1),
        is_in_scope=True,
        full_text="The SEC's 2026 Enforcement Manual update...",
    )
    check("upsert_article returns an id", isinstance(aid, int) and aid > 0)

    # Duplicate URL → same id
    aid2 = db.upsert_article(
        firm_name="Gibson Dunn",
        title="SEC Updates Enforcement Manual (dup)",
        url="https://gibsondunn.com/sec-enforcement-manual-2026",
        date_published=_date(1),
        is_in_scope=True,
    )
    check("Duplicate URL returns existing id", aid2 == aid, f"{aid2} == {aid}")

    # article_exists
    check("article_exists True", db.article_exists("https://gibsondunn.com/sec-enforcement-manual-2026"))
    check("article_exists False for unknown URL", not db.article_exists("https://example.com/nope"))

    # get_article_by_url
    row = db.get_article_by_url("https://gibsondunn.com/sec-enforcement-manual-2026")
    check("get_article_by_url returns row", row is not None)
    check("Article firm_name correct", row["firm_name"] == "Gibson Dunn")
    check("Article is_in_scope correct", bool(row["is_in_scope"]) is True)

    # Insert out-of-scope article
    db.upsert_article(
        firm_name="Latham & Watkins",
        title="New IP Filing Deadlines",
        url="https://lw.com/ip-deadlines-2026",
        date_published=_date(2),
        is_in_scope=False,
    )

    # get_in_scope_articles_since
    cutoff = _iso(3)
    in_scope = db.get_in_scope_articles_since(cutoff)
    check("get_in_scope_articles_since filters correctly",
          all(bool(r["is_in_scope"]) for r in in_scope))
    check("In-scope count is 1 (out-of-scope excluded)", len(in_scope) == 1, str(len(in_scope)))

    # Add more articles for cluster tests
    for i, (firm, url_suffix) in enumerate([
        ("Latham & Watkins",   "sec-manual-latham"),
        ("Davis Polk",         "sec-manual-davispolk"),
        ("Kirkland & Ellis",   "sec-manual-kirkland"),
    ]):
        db.upsert_article(
            firm_name=firm,
            title=f"SEC Enforcement Manual — {firm}",
            url=f"https://example.com/{url_suffix}",
            date_published=_date(i),
            is_in_scope=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Test group 3 — Clusters CRUD
# ══════════════════════════════════════════════════════════════════════════════

def test_clusters():
    print("\n── DB: clusters ──")

    cid = db.upsert_cluster(
        subject_key="sec_enforcement_manual_formal_order_2026",
        description="SEC 2026 Enforcement Manual: Commission approval for Formal Orders",
    )
    check("upsert_cluster returns id", isinstance(cid, int) and cid > 0)

    # Idempotent
    cid2 = db.upsert_cluster(
        subject_key="sec_enforcement_manual_formal_order_2026",
        description="Updated description should not overwrite",
    )
    check("upsert_cluster idempotent", cid2 == cid, f"{cid2} == {cid}")

    # get_cluster_by_key
    c = db.get_cluster_by_key("sec_enforcement_manual_formal_order_2026")
    check("get_cluster_by_key returns row", c is not None)
    check("Cluster status defaults to active", c["status"] == "active")

    # Link articles to cluster
    with db.get_conn() as conn:
        article_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM articles WHERE is_in_scope = 1 LIMIT 4"
            ).fetchall()
        ]
    for aid in article_ids:
        db.link_article_to_cluster(cid, aid)

    # Idempotent link
    db.link_article_to_cluster(cid, article_ids[0])

    # get_cluster_articles
    arts = db.get_cluster_articles(cid)
    check("Cluster has correct article count", len(arts) == len(article_ids), str(len(arts)))

    # refresh_cluster_stats
    db.refresh_cluster_stats(cid)
    c = db.get_cluster_by_key("sec_enforcement_manual_formal_order_2026")
    check("Cluster firm_count >= 3 after refresh",
          c["firm_count"] >= 3, str(c["firm_count"]))
    check("Cluster has earliest_pub_date", c["earliest_pub_date"] is not None)
    check("Cluster has latest_pub_date", c["latest_pub_date"] is not None)

    # get_active_clusters
    active = db.get_active_clusters()
    check("get_active_clusters returns at least 1", len(active) >= 1)

    # get_cluster_firm_names
    firms = db.get_cluster_firm_names(cid)
    check("get_cluster_firm_names returns list", isinstance(firms, list))
    check("Firm names non-empty", len(firms) >= 1)

    # lock_cluster
    db.lock_cluster(cid, "drafts/test_draft.docx")
    c = db.get_cluster_by_key("sec_enforcement_manual_formal_order_2026")
    check("Cluster status is locked", c["status"] == "locked")
    check("Cluster locked_at is set", c["locked_at"] is not None)
    check("Cluster draft_file is set", c["draft_file"] == "drafts/test_draft.docx")

    # locked cluster no longer in active list
    active_after = db.get_active_clusters()
    active_keys = [r["subject_key"] for r in active_after]
    check("Locked cluster absent from active list",
          "sec_enforcement_manual_formal_order_2026" not in active_keys)


# ══════════════════════════════════════════════════════════════════════════════
# Test group 4 — Scrape log
# ══════════════════════════════════════════════════════════════════════════════

def test_scrape_log():
    print("\n── DB: scrape_log ──")

    db.log_scrape("Gibson Dunn", "https://gibsondunn.com/insights/", True, 12)
    db.log_scrape("Latham & Watkins", "https://lw.com/insights/", False,
                  0, "Timeout after 30s")

    last = db.get_last_scrape("Gibson Dunn")
    check("get_last_scrape returns row", last is not None)
    check("Scrape success recorded", bool(last["success"]) is True)
    check("articles_found recorded", last["articles_found"] == 12)

    errors = db.get_scrape_errors_since(_iso(1))
    check("get_scrape_errors_since returns at least 1 error", len(errors) >= 1)
    check("Error message recorded", errors[0]["error_message"] == "Timeout after 30s")


# ══════════════════════════════════════════════════════════════════════════════
# Test group 5 — State manager (alert_tracker.json)
# ══════════════════════════════════════════════════════════════════════════════

def test_state_manager_tracker():
    print("\n── StateManager: alert_tracker.json ──")

    # Load on empty file → default structure
    t = sm.load_tracker()
    check("load_tracker returns dict with 'clusters'", "clusters" in t)

    # Add articles to a cluster
    sm.tracker_add_article(
        subject_key="sec_settlement_policy_2026",
        description="SEC simultaneous settlement / collateral waiver policy",
        firm_name="Gibson Dunn",
        title="SEC Chairman Atkins Restores Simultaneous Settlement",
        url="https://gibsondunn.com/atkins-settlement-2026",
        date_detected=_iso(1),
        date_published=_date(1),
    )
    sm.tracker_add_article(
        subject_key="sec_settlement_policy_2026",
        description="SEC simultaneous settlement / collateral waiver policy",
        firm_name="Latham & Watkins",
        title="Changes to SEC Settlement Procedures",
        url="https://lw.com/sec-settlement-2026",
        date_detected=_iso(1),
        date_published=_date(1),
    )
    sm.tracker_add_article(
        subject_key="sec_settlement_policy_2026",
        description="SEC simultaneous settlement / collateral waiver policy",
        firm_name="Davis Polk",
        title="SEC Enforcement: Settlement Policy Update",
        url="https://davispolk.com/sec-settlement-2026",
        date_detected=_iso(),
        date_published=_date(),
    )

    # Verify persisted state
    t = sm.load_tracker()
    cluster = t["clusters"].get("sec_settlement_policy_2026")
    check("Cluster present in tracker", cluster is not None)
    check("firm_count = 3", cluster["firm_count"] == 3, str(cluster["firm_count"]))
    check("articles list has 3 entries", len(cluster["articles"]) == 3)

    # Deduplication
    sm.tracker_add_article(
        subject_key="sec_settlement_policy_2026",
        description="...",
        firm_name="Gibson Dunn",
        title="Duplicate",
        url="https://gibsondunn.com/atkins-settlement-2026",  # same URL
        date_detected=_iso(),
        date_published=_date(),
    )
    t = sm.load_tracker()
    cluster = t["clusters"]["sec_settlement_policy_2026"]
    check("Duplicate URL not added again", len(cluster["articles"]) == 3)

    # tracker_get_cluster
    c = sm.tracker_get_cluster("sec_settlement_policy_2026")
    check("tracker_get_cluster returns dict", isinstance(c, dict))
    check("tracker_get_cluster returns None for unknown key",
          sm.tracker_get_cluster("nonexistent") is None)

    # tracker_all_clusters
    all_c = sm.tracker_all_clusters()
    check("tracker_all_clusters returns list", isinstance(all_c, list))
    check("tracker_all_clusters has at least 1 entry", len(all_c) >= 1)


# ══════════════════════════════════════════════════════════════════════════════
# Test group 6 — State manager (drafted_topics.json)
# ══════════════════════════════════════════════════════════════════════════════

def test_state_manager_drafted():
    print("\n── StateManager: drafted_topics.json ──")

    # Initially not locked
    check("is_topic_locked False before lock",
          not sm.is_topic_locked("sec_settlement_policy_2026"))

    # Lock it
    sm.lock_topic(
        subject_key="sec_settlement_policy_2026",
        description="SEC simultaneous settlement / collateral waiver policy",
        draft_file="drafts/sec_settlement_policy_2026_20260314.docx",
        trigger_firms=["Gibson Dunn", "Latham & Watkins", "Davis Polk"],
    )

    check("is_topic_locked True after lock",
          sm.is_topic_locked("sec_settlement_policy_2026"))

    # get_lock_info
    info = sm.get_lock_info("sec_settlement_policy_2026")
    check("get_lock_info returns dict", isinstance(info, dict))
    check("lock_info has locked_at", "locked_at" in info)
    check("lock_info has trigger_firms", info["trigger_firms"] == [
        "Gibson Dunn", "Latham & Watkins", "Davis Polk"
    ])
    check("get_lock_info None for unlocked key",
          sm.get_lock_info("nonexistent") is None)

    # get_all_locked_topics
    locked = sm.get_all_locked_topics()
    check("get_all_locked_topics returns dict", isinstance(locked, dict))
    check("Locked topic appears in all_locked_topics",
          "sec_settlement_policy_2026" in locked)

    # Verify alert_tracker.json also updated
    t = sm.load_tracker()
    cluster = t["clusters"].get("sec_settlement_policy_2026")
    check("alert_tracker.json reflects locked status",
          cluster is not None and cluster.get("status") == "locked")

    # Confirm drafted_topics.json is valid JSON on disk
    with open(sm.DRAFTED_TOPICS_FILE, "r") as f:
        raw = json.load(f)
    check("drafted_topics.json is valid JSON", isinstance(raw, dict))


# ══════════════════════════════════════════════════════════════════════════════
# Test group 7 — rebuild_tracker_from_db
# ══════════════════════════════════════════════════════════════════════════════

def test_rebuild():
    print("\n── StateManager: rebuild_tracker_from_db ──")
    # Temporarily delete the tracker and rebuild from DB
    if sm.ALERT_TRACKER_FILE.exists():
        sm.ALERT_TRACKER_FILE.unlink()
    sm.rebuild_tracker_from_db()
    t = sm.load_tracker()
    check("Rebuilt tracker is a dict with 'clusters'", "clusters" in t)
    # Cluster we locked in DB tests should be present
    check("Locked cluster present after rebuild",
          "sec_enforcement_manual_formal_order_2026" in t["clusters"])


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def run_all():
    tests = [
        test_db_init,
        test_articles,
        test_clusters,
        test_scrape_log,
        test_state_manager_tracker,
        test_state_manager_drafted,
        test_rebuild,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  [ASSERTION] {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{passed+failed} test groups passed")
    if failed:
        print(f"  {failed} group(s) FAILED — check output above")
    else:
        print("  All tests PASSED ✅")
    print(f"{'='*60}\n")

    # Cleanup temp dir
    shutil.rmtree(_TMPDIR, ignore_errors=True)

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
