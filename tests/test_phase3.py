"""
tests/test_phase3.py
Phase 3 scraper test — live network calls against 5 real law firm sites
of varying complexity.

Run:
    venv/bin/python tests/test_phase3.py

Pass criteria:
  - At least 4 of 5 firms return ≥1 article  (network issues can cause 1 miss)
  - No unhandled exceptions (errors are captured in FirmScrapeResult.error)
  - All results are persisted in the test DB

Firms chosen to represent a spread of website architectures:
  1. Cleary Gottlieb     — static-ish, clean structure
  2. Skadden             — JS-heavy BigLaw site
  3. Foley & Lardner     — mid-size, standard CMS
  4. Holland & Knight    — enterprise law firm CMS
  5. Cooley              — modern React/JS-heavy site
"""

import os
import sys
import tempfile
import shutil
import asyncio
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")

# Redirect DB to temp dir
import src.config as _cfg
_TMPDIR = tempfile.mkdtemp(prefix="alert_scraper_test_")
_cfg.DB_FILE            = Path(_TMPDIR) / "test.db"
_cfg.ALERT_TRACKER_FILE = Path(_TMPDIR) / "alert_tracker.json"
_cfg.DRAFTED_TOPICS_FILE= Path(_TMPDIR) / "drafted_topics.json"
_cfg.DATA_DIR           = Path(_TMPDIR)
_cfg.DRAFTS_DIR         = Path(_TMPDIR) / "drafts"
_cfg.DRAFTS_DIR.mkdir(exist_ok=True)

from src import database as db
from src.scraper import (
    run_scrape, extract_articles_from_html,
    _parse_date_str, _date_from_url, _is_article_url,
    FirmScrapeResult, ScrapedArticle,
)
from urllib.parse import urlparse

# ── Test firms ─────────────────────────────────────────────────────────────────

TEST_FIRMS = [
    {
        "name": "Cleary Gottlieb Steen & Hamilton",
        "url": "https://www.clearygottlieb.com/news-and-insights/publication-listing",
    },
    {
        "name": "Skadden, Arps, Slate, Meagher & Flom",
        "url": "https://www.skadden.com/insights/publications",
    },
    {
        "name": "Foley & Lardner",
        "url": "https://www.foley.com/insights/",
    },
    {
        "name": "Holland & Knight",
        "url": "https://www.hklaw.com/en/insights",
    },
    {
        "name": "Cooley",
        "url": "https://www.cooley.com/news/insight",
    },
]

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


# ── Helpers ────────────────────────────────────────────────────────────────────

def _check(label: str, cond: bool, detail: str = "") -> bool:
    icon = PASS if cond else FAIL
    suffix = f"  [{detail}]" if detail else ""
    print(f"  {icon}  {label}{suffix}")
    return cond


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests (no network)
# ══════════════════════════════════════════════════════════════════════════════

def test_date_parsing():
    print("\n── Date parsing (unit) ──")
    cases = [
        ("March 14, 2026",             "2026-03-14"),
        ("Mar 14, 2026",               "2026-03-14"),
        ("14 March 2026",              "2026-03-14"),
        ("2026-03-14",                 "2026-03-14"),
        ("Published: February 3, 2026","2026-02-03"),
        ("no date here at all",        None),
        ("1999-01-01",                 None),   # below _MIN_YEAR
    ]
    passed = all(
        _check(f"parse({raw!r}) == {expected!r}", _parse_date_str(raw) == expected)
        for raw, expected in cases
    )
    return passed


def test_url_date():
    print("\n── URL date extraction (unit) ──")
    cases = [
        ("https://example.com/2026/03/14/article", "2026-03-14"),
        ("https://example.com/2026/03/article",    "2026-03-01"),
        ("https://example.com/insights/title",     None),
    ]
    passed = all(
        _check(f"url_date({url!r}) == {expected!r}", _date_from_url(url) == expected)
        for url, expected in cases
    )
    return passed


def test_url_filter():
    print("\n── URL article filter (unit) ──")
    base = urlparse("https://www.gibsondunn.com/insights/")

    should_pass = [
        "https://www.gibsondunn.com/sec-enforcement-update-march-2026/",  # flat slug
        "https://www.gibsondunn.com/insights/publications/2026/03/sec-manual",
        "https://www.gibsondunn.com/insights/sec-doj-white-collar-alert",  # 2 segments
    ]
    should_fail = [
        "https://other-domain.com/insights/article",        # wrong domain
        "https://www.gibsondunn.com/",                      # root
        "https://www.gibsondunn.com/insights/",             # listing page
        "https://www.gibsondunn.com/sec/",                  # single short segment
        "https://www.gibsondunn.com/people/john-smith",     # people page
        "https://www.gibsondunn.com/report.pdf",            # file
        "#section",                                          # fragment
    ]

    all_pass = True
    for url in should_pass:
        ok = _is_article_url(url, base)
        all_pass &= _check(f"PASS  {url}", ok)
    for url in should_fail:
        ok = not _is_article_url(url, base)
        all_pass &= _check(f"REJECT {url}", ok)
    return all_pass


def test_html_extraction():
    print("\n── HTML extraction (unit, synthetic HTML) ──")

    SAMPLE_HTML = """
    <html><body>
      <nav><a href="/insights/">All Insights</a><a href="/people/john">John Smith</a></nav>
      <main>
        <ul>
          <li>
            <a href="/insights/2026/03/sec-enforcement-manual-update">
              SEC Enforcement Manual: Commission Approval for Formal Orders
            </a>
            <span class="date">March 14, 2026</span>
          </li>
          <li>
            <a href="/insights/2026/03/doj-cooperation-policy-shift">
              DOJ Issues New Guidance on Cooperation Credit in White Collar Cases
            </a>
            <time datetime="2026-03-10">March 10, 2026</time>
          </li>
          <li>
            <a href="/insights/topics/sec">SEC Topics</a>
          </li>
        </ul>
      </main>
      <footer><a href="/privacy-policy">Privacy</a></footer>
    </body></html>
    """

    articles = extract_articles_from_html(
        SAMPLE_HTML,
        "https://www.testfirm.com/insights/",
        "Test Firm",
    )

    ok1 = _check("Found exactly 2 articles (nav + footer + category filtered out)",
                 len(articles) == 2, str(len(articles)))
    ok2 = ok3 = ok4 = False
    if articles:
        ok2 = _check("First article title extracted correctly",
                     "SEC Enforcement Manual" in articles[0].title)
        ok3 = _check("First article date extracted (span.date)",
                     articles[0].date_published == "2026-03-14",
                     str(articles[0].date_published))
        ok4 = _check("Second article date from <time> element",
                     articles[1].date_published == "2026-03-10",
                     str(articles[1].date_published))
    return all([ok1, ok2, ok3, ok4])


# ══════════════════════════════════════════════════════════════════════════════
# Live scrape test (network required)
# ══════════════════════════════════════════════════════════════════════════════

def test_live_scrape():
    print("\n── Live scrape — 5 firms ──")
    print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")

    db.init_db()
    results = run_scrape(firms=TEST_FIRMS)

    print(f"\n  {'Firm':<45} {'Status':<8} {'Articles':>8}  {'Dates':>6}  Notes")
    print(f"  {'-'*45} {'-'*8} {'-'*8}  {'-'*6}  {'-'*30}")

    success_with_articles = 0
    all_ok = True

    for r in results:
        status = "OK" if r.success else "FAIL"
        article_count = len(r.articles)
        date_count = sum(1 for a in r.articles if a.date_published)
        note = r.error[:50] if r.error else ""

        icon = PASS if (r.success and article_count >= 1) else (WARN if r.success else FAIL)
        print(f"  {icon}  {r.firm_name[:43]:<43}  {status:<6}  {article_count:>5}  {date_count:>5}  {note}")

        if r.success and article_count >= 1:
            success_with_articles += 1

    print()

    # Show sample articles from first successful result
    for r in results:
        if r.success and r.articles:
            print(f"  Sample articles from {r.firm_name}:")
            for art in r.articles[:3]:
                date_str = art.date_published or "no date"
                print(f"    [{date_str}] {art.title[:80]}")
            break

    print()

    # Verify DB persistence
    with db.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    _check(f"Articles persisted to DB: {total}", total >= success_with_articles)

    # Pass condition: ≥4 of 5 firms return at least 1 article
    passed_threshold = success_with_articles >= 4
    all_ok = _check(
        f"At least 4/5 firms returned articles ({success_with_articles}/5)",
        passed_threshold,
    )

    print(f"\n  Finished: {datetime.now().strftime('%H:%M:%S')}")
    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def run_all():
    unit_tests = [
        test_date_parsing,
        test_url_date,
        test_url_filter,
        test_html_extraction,
    ]
    live_tests = [test_live_scrape]

    passed = failed = 0

    print("\n=== Phase 3 Scraper Tests ===")
    print("Running unit tests first (no network)...")
    for t in unit_tests:
        try:
            ok = t()
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  {FAIL}  EXCEPTION in {t.__name__}: {e}")
            failed += 1

    print("\nRunning live scrape (network required, ~60–90 s)...")
    for t in live_tests:
        try:
            ok = t()
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  {FAIL}  EXCEPTION in {t.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{passed+failed} test groups passed")
    if failed:
        print(f"  {failed} group(s) FAILED")
    else:
        print("  All tests PASSED ✅")
    print(f"{'='*50}\n")

    shutil.rmtree(_TMPDIR, ignore_errors=True)
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
