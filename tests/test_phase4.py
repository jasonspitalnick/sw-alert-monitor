"""
tests/test_phase4.py
Phase 4 tests: classifier + cluster detector.

Structure
---------
Group 1–3: pure unit tests for the detector (no network, no API key needed)
Group 4:   live Anthropic API test — classifies 6 synthetic articles and
           verifies that the detector then flags the resulting cluster

Run:
    venv/bin/python tests/test_phase4.py

Requirements:
    ANTHROPIC_API_KEY must be set (real key) for group 4.
"""

import os
import sys
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Redirect all data files to temp dir ───────────────────────────────────────
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")

import src.config as _cfg
_TMPDIR = tempfile.mkdtemp(prefix="alert_phase4_test_")
_cfg.DB_FILE             = Path(_TMPDIR) / "test.db"
_cfg.ALERT_TRACKER_FILE  = Path(_TMPDIR) / "alert_tracker.json"
_cfg.DRAFTED_TOPICS_FILE = Path(_TMPDIR) / "drafted_topics.json"
_cfg.DATA_DIR            = Path(_TMPDIR)
_cfg.DRAFTS_DIR          = Path(_TMPDIR) / "drafts"
_cfg.DRAFTS_DIR.mkdir(exist_ok=True)

from src import database as db
from src import state_manager as sm
from src.cluster_detector import (
    count_effective_firms,
    check_cluster_saturation,
    find_saturated_clusters,
    _find_saturating_window,
    normalise_firm,
)
from src.classifier import classify_article, classify_new_articles, _sanitise_key

PASS = "✅"
FAIL = "❌"

_passed = _failed = 0

def check(label: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    icon = PASS if cond else FAIL
    suffix = f"  [{detail}]" if detail else ""
    print(f"  {icon}  {label}{suffix}")
    if cond:
        _passed += 1
    else:
        _failed += 1
    return cond


def _iso(days_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(days_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════════════
# Group 1 — Merged-firm normalisation
# ══════════════════════════════════════════════════════════════════════════════

def test_firm_normalisation():
    print("\n── Merged-firm normalisation ──")

    check("Locke Lord normalises to canonical",
          normalise_firm("Locke Lord") == "Locke Lord")
    check("Troutman Pepper normalises to Locke Lord",
          normalise_firm("Troutman Pepper") == "Locke Lord")
    check("Unrelated firm unchanged",
          normalise_firm("Gibson Dunn") == "Gibson Dunn")

    count, firms = count_effective_firms(
        ["Locke Lord", "Troutman Pepper", "Gibson Dunn"]
    )
    check("Locke Lord + Troutman Pepper + Gibson = 2 effective firms",
          count == 2, str(count))
    check("Effective firm list deduplicated correctly",
          set(firms) == {"Locke Lord", "Gibson Dunn"})

    count2, _ = count_effective_firms(
        ["Gibson Dunn", "Latham & Watkins", "Davis Polk"]
    )
    check("3 unrelated firms = 3 effective", count2 == 3, str(count2))

    count3, _ = count_effective_firms(["Locke Lord", "Troutman Pepper"])
    check("Merged pair = 1 effective", count3 == 1, str(count3))


# ══════════════════════════════════════════════════════════════════════════════
# Group 2 — Sliding window algorithm
# ══════════════════════════════════════════════════════════════════════════════

def test_window_algorithm():
    print("\n── Sliding window algorithm ──")

    # 3 firms, dates within 5-day window → should trigger
    within = [
        ("Gibson Dunn",       _date(4)),
        ("Latham & Watkins",  _date(2)),
        ("Davis Polk",        _date(0)),
    ]
    result = _find_saturating_window(within)
    check("3 firms within 4 days → window found", result is not None)

    # 3 firms, dates spread 6 days → should NOT trigger
    spread = [
        ("Gibson Dunn",       _date(6)),
        ("Latham & Watkins",  _date(3)),
        ("Davis Polk",        _date(0)),
    ]
    result2 = _find_saturating_window(spread)
    check("3 firms spread 6 days → no window", result2 is None)

    # Exactly on 5-day boundary (inclusive) → should trigger
    boundary = [
        ("Gibson Dunn",      _date(5)),
        ("Latham & Watkins", _date(2)),
        ("Davis Polk",       _date(0)),
    ]
    result3 = _find_saturating_window(boundary)
    check("3 firms exactly 5 days apart → window found", result3 is not None)

    # 2 firms only → should NOT trigger (need 3)
    two_firms = [
        ("Gibson Dunn",      _date(1)),
        ("Latham & Watkins", _date(0)),
    ]
    result4 = _find_saturating_window(two_firms)
    check("Only 2 firms → no window", result4 is None)

    # Merged firms counted as 1 → should NOT trigger (effectively 2 firms)
    merged = [
        ("Locke Lord",       _date(1)),
        ("Troutman Pepper",  _date(0)),
        ("Gibson Dunn",      _date(1)),
    ]
    result5 = _find_saturating_window(merged)
    check("Locke Lord + Troutman Pepper + Gibson = 2 effective → no trigger",
          result5 is None)

    # 4 firms, 3 within window, 1 outside → should trigger on the 3
    mixed = [
        ("Firm A",  _date(10)),  # outside any window containing B, C, D
        ("Firm B",  _date(3)),
        ("Firm C",  _date(2)),
        ("Firm D",  _date(0)),
    ]
    result6 = _find_saturating_window(mixed)
    check("4 firms, 3 within window → triggers", result6 is not None)


# ══════════════════════════════════════════════════════════════════════════════
# Group 3 — End-to-end detector with DB
# ══════════════════════════════════════════════════════════════════════════════

def test_detector_with_db():
    print("\n── Detector end-to-end (DB-backed) ──")
    db.init_db()

    # Create a cluster manually
    cid = db.upsert_cluster(
        "sec_enforcement_manual_formal_order_2026",
        "SEC 2026 Enforcement Manual: Commission approval for Formal Orders",
    )

    # Add 3 articles from 3 different firms, dated within 4 days
    firms = ["Gibson Dunn", "Latham & Watkins", "Davis Polk"]
    for i, firm in enumerate(firms):
        aid = db.upsert_article(
            firm_name=firm,
            title=f"SEC Enforcement Manual Update — {firm}",
            url=f"https://example.com/{firm.lower().replace(' ', '-')}-sec-manual",
            date_published=_date(i),
            is_in_scope=True,
        )
        db.link_article_to_cluster(cid, aid)

    db.refresh_cluster_stats(cid)

    result = check_cluster_saturation(cid)
    check("Cluster with 3 firms / 2-day spread triggers", result is not None)

    if result:
        check("effective_firm_count == 3",
              result.effective_firm_count == 3, str(result.effective_firm_count))
        check("subject_key correct",
              result.subject_key == "sec_enforcement_manual_formal_order_2026")

    # find_saturated_clusters should find it
    saturated = find_saturated_clusters()
    check("find_saturated_clusters returns 1 result",
          len(saturated) == 1, str(len(saturated)))

    # Lock the cluster — detector should not re-flag it
    db.lock_cluster(cid, "drafts/test.docx")
    saturated_after = find_saturated_clusters()
    check("Locked cluster not returned by find_saturated_clusters",
          all(r.cluster_id != cid for r in saturated_after))

    # A cluster with only 2 firms should not trigger
    cid2 = db.upsert_cluster("doj_test_cluster", "DOJ test")
    for i, firm in enumerate(["Gibson Dunn", "Kirkland & Ellis"]):
        aid = db.upsert_article(
            firm_name=firm,
            title=f"DOJ Policy — {firm}",
            url=f"https://example.com/doj-{i}",
            date_published=_date(1),
            is_in_scope=True,
        )
        db.link_article_to_cluster(cid2, aid)
    db.refresh_cluster_stats(cid2)

    result2 = check_cluster_saturation(cid2)
    check("Cluster with only 2 firms does not trigger", result2 is None)


# ══════════════════════════════════════════════════════════════════════════════
# Group 4 — Live Anthropic API classification
# ══════════════════════════════════════════════════════════════════════════════

def test_live_classification():
    print("\n── Live classifier (Anthropic API) ──")

    if not _cfg.ANTHROPIC_API_KEY:
        print("  ⚠️   ANTHROPIC_API_KEY not set — skipping live test")
        return True

    # Insert 6 synthetic articles:
    # 3 in-scope and narrow (same topic) → should cluster together
    # 2 in-scope but broad → should be in-scope but not clustered with narrow ones
    # 1 out-of-scope → should be classified out-of-scope

    IN_SCOPE_ARTICLES = [
        {
            "firm": "Gibson Dunn",
            "title": "SEC Enforcement Manual Update: Commission Must Now Approve All Formal Orders of Investigation",
            "url": "https://www.gibsondunn.com/sec-enforcement-manual-formal-order-2026",
            "date": _date(2),
        },
        {
            "firm": "Latham & Watkins",
            "title": "SEC's 2026 Enforcement Manual Revokes Delegated Authority for Formal Orders",
            "url": "https://www.lw.com/insights/sec-enforcement-manual-2026",
            "date": _date(1),
        },
        {
            "firm": "Davis Polk",
            "title": "New SEC Policy Requires Full Commission Vote Before Formal Investigation Can Begin",
            "url": "https://www.davispolk.com/sec-formal-orders-2026",
            "date": _date(0),
        },
    ]

    BROAD_ARTICLES = [
        {
            "firm": "Cleary Gottlieb",
            "title": "Quarterly SEC Enforcement Review: Q1 2026",
            "url": "https://www.clearygottlieb.com/quarterly-sec-review-q1-2026",
            "date": _date(1),
        },
    ]

    OUT_OF_SCOPE = [
        {
            "firm": "Littler Mendelson",
            "title": "New NLRB Rules on Union Election Procedures",
            "url": "https://www.littler.com/nlrb-union-elections-2026",
            "date": _date(0),
        },
    ]

    all_test_articles = IN_SCOPE_ARTICLES + BROAD_ARTICLES + OUT_OF_SCOPE

    # Insert into DB (unclassified)
    article_ids = []
    for art in all_test_articles:
        aid = db.upsert_article(
            firm_name=art["firm"],
            title=art["title"],
            url=art["url"],
            date_published=art["date"],
            is_in_scope=False,
        )
        article_ids.append(aid)

    print(f"  Inserted {len(article_ids)} test articles, classifying...")

    # Run bulk classifier
    results = classify_new_articles(batch_limit=10)

    # len(results) may be > 5 if earlier test groups left unclassified DB rows
    check("At least 5 articles classified (our test batch)",
          len(results) >= len(all_test_articles), str(len(results)))

    # Check in-scope narrow articles got the same cluster
    narrow_results = [
        r for r in results
        if any(a["url"] == _get_url(r) for a in IN_SCOPE_ARTICLES)
    ]
    narrow_keys = {r.subject_key for r in narrow_results if r.subject_key}
    print(f"  Narrow article cluster keys: {narrow_keys}")
    check("All 3 narrow articles in-scope",
          all(r.is_in_scope for r in narrow_results),
          str([r.is_in_scope for r in narrow_results]))
    check("All 3 narrow articles share exactly 1 cluster key",
          len(narrow_keys) == 1, str(narrow_keys))

    # Out-of-scope article should be classified correctly
    oos_result = next(
        (r for r in results if r.firm_name == "Littler Mendelson"), None
    )
    check("Out-of-scope article classified correctly",
          oos_result is not None and not oos_result.is_in_scope)

    # Now run the saturation detector
    saturated = find_saturated_clusters()
    print(f"  Saturated clusters found: {[s.subject_key for s in saturated]}")
    check("Saturation detector fires on the 3-firm narrow cluster",
          len(saturated) >= 1)

    if saturated:
        s = saturated[0]
        check("Saturated cluster has ≥3 effective firms",
              s.effective_firm_count >= 3, str(s.effective_firm_count))
        check("Saturated cluster window ≤5 days",
              s.window_days <= 5, str(s.window_days))

    return True


def _get_url(result) -> str:
    """Look up the URL for a ClassificationResult from the DB."""
    a = db.get_article_by_id(result.article_id)
    return a["url"] if a else ""


# ══════════════════════════════════════════════════════════════════════════════
# Group 5 — subject_key sanitisation
# ══════════════════════════════════════════════════════════════════════════════

def test_key_sanitisation():
    print("\n── subject_key sanitisation ──")
    check("Spaces → underscores",
          _sanitise_key("SEC Enforcement Manual 2026") == "sec_enforcement_manual_2026")
    check("Already clean key unchanged",
          _sanitise_key("sec_formal_order_2026") == "sec_formal_order_2026")
    check("Hyphens → underscores",
          _sanitise_key("sec-formal-order-2026") == "sec_formal_order_2026")
    check("Leading/trailing underscores stripped",
          _sanitise_key("_sec_2026_") == "sec_2026")
    check("Special chars removed",
          _sanitise_key("SEC: Formal Order (2026)") == "sec_formal_order_2026")


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def run_all():
    global _passed, _failed
    groups = [
        test_firm_normalisation,
        test_window_algorithm,
        test_detector_with_db,
        test_key_sanitisation,
        test_live_classification,   # API-dependent; runs last
    ]

    print("\n=== Phase 4 Tests: Classifier + Cluster Detector ===")

    group_passed = group_failed = 0
    for fn in groups:
        pre_p, pre_f = _passed, _failed
        try:
            fn()
            group_failed_count = _failed - pre_f
            if group_failed_count == 0:
                group_passed += 1
            else:
                group_failed += 1
        except Exception as e:
            import traceback
            print(f"  {FAIL}  EXCEPTION in {fn.__name__}: {e}")
            traceback.print_exc()
            group_failed += 1

    print(f"\n{'='*55}")
    print(f"Assertions: {_passed} passed, {_failed} failed")
    print(f"Groups:     {group_passed}/{group_passed+group_failed} passed")
    if _failed == 0:
        print("All tests PASSED ✅")
    print(f"{'='*55}\n")

    shutil.rmtree(_TMPDIR, ignore_errors=True)
    return _failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
