"""
tests/test_phase5.py
Phase 5 tests: S&W gap validator.

Structure
---------
Group 1 — Unit tests: data structures and prompt builder (no network, no API)
Group 2 — Unit tests: nonce extraction and AJAX card parser (no network)
Group 3 — Live httpx fetch of swlaw.com publications (no API key needed)
Group 4 — Live Anthropic API gap-check (requires ANTHROPIC_API_KEY)

Run:
    venv/bin/python tests/test_phase5.py

Requirements:
    ANTHROPIC_API_KEY must be set (real key) for Group 4.
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Redirect data files to temp dir ───────────────────────────────────────────
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")

import src.config as _cfg
_TMPDIR = tempfile.mkdtemp(prefix="alert_phase5_test_")
_cfg.DB_FILE             = Path(_TMPDIR) / "test.db"
_cfg.ALERT_TRACKER_FILE  = Path(_TMPDIR) / "alert_tracker.json"
_cfg.DRAFTED_TOPICS_FILE = Path(_TMPDIR) / "drafted_topics.json"
_cfg.DATA_DIR            = Path(_TMPDIR)
_cfg.DRAFTS_DIR          = Path(_TMPDIR) / "drafts"
_cfg.DRAFTS_DIR.mkdir(exist_ok=True)

from src.gap_validator import (
    SWArticle,
    GapValidationResult,
    validate_gap,
    fetch_sw_articles,
    _build_gap_prompt,
    _extract_nonce,
    _parse_ajax_cards,
)

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


# ══════════════════════════════════════════════════════════════════════════════
# Group 1 — Data structures and prompt builder
# ══════════════════════════════════════════════════════════════════════════════

def test_data_structures():
    print("\n── Data structures ──")

    a = SWArticle(
        title="SEC Enforcement Manual Update 2026",
        url="https://www.swlaw.com/publication/sec-enforcement-manual-2026",
        date_published="2026-03-01",
    )
    check("SWArticle title", a.title == "SEC Enforcement Manual Update 2026")
    check("SWArticle url", "swlaw.com" in a.url)
    check("SWArticle date", a.date_published == "2026-03-01")

    a2 = SWArticle(title="Some Article", url="https://www.swlaw.com/publication/some-article")
    check("SWArticle date defaults to None", a2.date_published is None)

    # has_gap=True (no S&W coverage)
    r = GapValidationResult(
        subject_key="test_key",
        has_gap=True,
        sw_articles_checked=50,
        blocking_article=None,
        reasoning="No matching article found.",
    )
    check("GapValidationResult has_gap=True", r.has_gap is True)
    check("GapValidationResult blocking=None", r.blocking_article is None)
    check("GapValidationResult articles_checked=50", r.sw_articles_checked == 50)

    # has_gap=False (S&W already covered it)
    r2 = GapValidationResult(
        subject_key="sec_enforcement_manual_2026",
        has_gap=False,
        sw_articles_checked=30,
        blocking_article=a,
        reasoning="Article #1 is a dedicated alert on this topic.",
    )
    check("GapValidationResult has_gap=False", r2.has_gap is False)
    check("GapValidationResult blocking populated", r2.blocking_article is not None)
    check("GapValidationResult blocking title correct",
          r2.blocking_article.title == "SEC Enforcement Manual Update 2026")


def test_prompt_builder():
    print("\n── Prompt builder ──")

    articles = [
        SWArticle("SEC Enforcement Manual Update", "https://swlaw.com/publication/1", "2026-03-01"),
        SWArticle("Quarterly Roundup Q1 2026",    "https://swlaw.com/publication/2", "2026-02-15"),
        SWArticle("DOJ Cooperation Credit Policy", "https://swlaw.com/publication/3", None),
    ]

    prompt = _build_gap_prompt(
        subject_key="sec_enforcement_manual_formal_order_2026",
        subject_description=(
            "SEC 2026 Enforcement Manual requires Commission approval "
            "for Formal Orders of Investigation"
        ),
        sw_articles=articles,
    )

    check("Prompt contains subject_key",
          "sec_enforcement_manual_formal_order_2026" in prompt)
    check("Prompt contains subject_description",
          "Commission approval" in prompt)
    check("Prompt lists article 1",
          "SEC Enforcement Manual Update" in prompt)
    check("Prompt lists article 2",
          "Quarterly Roundup Q1 2026" in prompt)
    check("Prompt lists article 3",
          "DOJ Cooperation Credit Policy" in prompt)
    check("Prompt uses 1-based numbering",
          "  1." in prompt and "  2." in prompt and "  3." in prompt)
    check("Prompt shows date for article 1",
          "2026-03-01" in prompt)
    check("Prompt shows n/a for article with no date",
          "n/a" in prompt)
    check("Prompt requests JSON response",
          "has_dedicated_alert" in prompt)


def test_validate_gap_empty_articles():
    print("\n── validate_gap with empty article list ──")

    result = validate_gap(
        subject_key="sec_formal_order_2026",
        subject_description="SEC requires Commission approval for Formal Orders",
        sw_articles=[],
    )

    check("Empty list → has_gap=True (safe default)",
          result.has_gap is True)
    check("sw_articles_checked == 0", result.sw_articles_checked == 0)
    check("blocking_article is None", result.blocking_article is None)
    check("reasoning is non-empty", bool(result.reasoning))


# ══════════════════════════════════════════════════════════════════════════════
# Group 2 — Nonce extraction and AJAX card parser
# ══════════════════════════════════════════════════════════════════════════════

def test_nonce_extraction():
    print("\n── Nonce extraction ──")

    html_with_nonce = """
    <html><body>
    <form>
      <input type="hidden" id="_wpnonce" name="_wpnonce" value="abc123def4">
      <input type="hidden" name="_wp_http_referer" value="/publications/?all">
    </form>
    </body></html>
    """
    nonce = _extract_nonce(html_with_nonce)
    check("Nonce extracted correctly", nonce == "abc123def4", str(nonce))

    html_no_nonce = "<html><body><p>No nonce here</p></body></html>"
    check("Returns None when no nonce present",
          _extract_nonce(html_no_nonce) is None)

    # Longer realistic nonce
    html_real = """<input type="hidden" id="_wpnonce" name="_wpnonce" value="c5d4ef2e34">"""
    check("Real-length nonce (10 hex chars) extracted",
          _extract_nonce(html_real) == "c5d4ef2e34")


def test_ajax_card_parser():
    print("\n── AJAX card parser ──")

    # Synthetic HTML matching the S&W card-news-list format
    html = """
    <div class="card-news-list">
        <div class="card-news-list__top">
            <div class="date">Mar 09, 2026</div>
            <div class="cat">Legal Alerts</div>
        </div>
        <a class="card-news-list__title"
           href="https://www.swlaw.com/publication/sec-enforcement-manual-formal-orders-2026/">
            <h3>SEC Enforcement Manual: New Formal Order Requirements</h3>
        </a>
    </div>
    <div class="card-news-list">
        <div class="card-news-list__top">
            <div class="date">Feb 20, 2026</div>
            <div class="cat">Legal Alerts</div>
        </div>
        <a class="card-news-list__title"
           href="https://www.swlaw.com/publication/doj-cooperation-credit-2026/">
            <h3>DOJ Updates Cooperation Credit Policy for 2026</h3>
        </a>
    </div>
    <div class="card-news-list">
        <div class="card-news-list__top">
            <div class="cat">Client Alerts</div>
        </div>
        <a class="card-news-list__title"
           href="https://www.swlaw.com/publication/tariff-update-2026/">
            <h3>Tariff Update March 2026</h3>
        </a>
    </div>
    """

    articles = _parse_ajax_cards(html)

    check("3 cards parsed", len(articles) == 3, str(len(articles)))

    check("Article 1 title correct",
          articles[0].title == "SEC Enforcement Manual: New Formal Order Requirements")
    check("Article 1 URL correct",
          "sec-enforcement-manual-formal-orders-2026" in articles[0].url)
    check("Article 1 date parsed to ISO",
          articles[0].date_published == "2026-03-09",
          str(articles[0].date_published))

    check("Article 2 title correct",
          articles[1].title == "DOJ Updates Cooperation Credit Policy for 2026")
    check("Article 2 date parsed",
          articles[1].date_published == "2026-02-20",
          str(articles[1].date_published))

    check("Article 3 (no date) date is None",
          articles[2].date_published is None)
    check("Article 3 title correct",
          articles[2].title == "Tariff Update March 2026")

    # Empty HTML returns empty list
    empty = _parse_ajax_cards("<div>no cards here</div>")
    check("Empty HTML returns empty list", len(empty) == 0)


# ══════════════════════════════════════════════════════════════════════════════
# Group 3 — Live httpx fetch of S&W publications
# ══════════════════════════════════════════════════════════════════════════════

def test_live_sw_fetch():
    print("\n── Live S&W publications fetch (httpx) ──")

    articles = fetch_sw_articles()

    check("Fetched at least 10 S&W articles",
          len(articles) >= 10, str(len(articles)))

    if articles:
        check("Each article has a non-empty title",
              all(bool(a.title.strip()) for a in articles))
        check("Each article has a non-empty URL",
              all(bool(a.url) for a in articles))
        check("All URLs contain 'swlaw.com'",
              all("swlaw.com" in a.url for a in articles))
        check("Articles are newest-first (or equal dates)",
              _dates_non_increasing(articles))

        print(f"\n  Sample articles ({len(articles)} total):")
        for a in articles[:5]:
            print(f"    [{a.date_published or 'n/a'}] {a.title[:70]}")

    return articles   # returned so Group 4 can reuse


def _dates_non_increasing(articles: list[SWArticle]) -> bool:
    """Return True if dates are in non-increasing order (newest first)."""
    dated = [a.date_published for a in articles if a.date_published]
    return all(dated[i] >= dated[i + 1] for i in range(len(dated) - 1))


# ══════════════════════════════════════════════════════════════════════════════
# Group 4 — Live Anthropic API gap-check
# ══════════════════════════════════════════════════════════════════════════════

def test_live_gap_check(sw_articles=None):
    print("\n── Live gap-check (Anthropic API) ──")

    if not _cfg.ANTHROPIC_API_KEY:
        print("  ⚠️   ANTHROPIC_API_KEY not set — skipping live API test")
        return True

    if sw_articles is None:
        print("  Fetching S&W articles for gap check...")
        sw_articles = fetch_sw_articles()

    if not sw_articles:
        print("  ⚠️   No S&W articles fetched — skipping API test")
        return True

    print(f"  Using {len(sw_articles)} S&W articles for gap check")

    # ── Test A: purely fictional subject → expect has_gap=True ───────────────
    result_a = validate_gap(
        subject_key="sec_synthetic_test_topic_xq9z_2026",
        subject_description=(
            "SEC's synthetic test order xq9z — "
            "fictional subject used for unit testing only"
        ),
        sw_articles=sw_articles,
    )

    check("Fictional subject → has_gap=True",
          result_a.has_gap is True, f"has_gap={result_a.has_gap}")
    check("Fictional subject → no blocking article",
          result_a.blocking_article is None)
    check("Reasoning field non-empty", bool(result_a.reasoning))
    check("sw_articles_checked > 0",
          result_a.sw_articles_checked > 0, str(result_a.sw_articles_checked))
    print(f"  Reasoning (fictional): {result_a.reasoning}")

    # ── Test B: subject that may or may not exist on S&W ─────────────────────
    # We just verify the result has the correct shape (not checking has_gap value
    # since we can't know what S&W may have published)
    result_b = validate_gap(
        subject_key="sec_enforcement_manual_formal_order_2026",
        subject_description=(
            "SEC's 2026 Enforcement Manual update now requires full Commission "
            "approval for Formal Orders of Investigation, revoking delegation "
            "to senior Enforcement staff"
        ),
        sw_articles=sw_articles,
    )

    check("Real subject result has bool has_gap",
          isinstance(result_b.has_gap, bool))
    check("Real subject sw_articles_checked > 0",
          result_b.sw_articles_checked > 0)
    check("Real subject reasoning non-empty",
          bool(result_b.reasoning))
    print(f"  has_gap (SEC formal order): {result_b.has_gap}")
    print(f"  Reasoning:                 {result_b.reasoning}")

    if not result_b.has_gap and result_b.blocking_article:
        print(f"  Blocking article: [{result_b.blocking_article.date_published}] "
              f"{result_b.blocking_article.title}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

def run_all():
    global _passed, _failed

    print("\n=== Phase 5 Tests: S&W Gap Validator ===")

    group_passed = group_failed = 0
    sw_articles_cache = None

    # Groups 1 & 2: pure unit tests (no network)
    unit_fns = [
        test_data_structures,
        test_prompt_builder,
        test_validate_gap_empty_articles,
        test_nonce_extraction,
        test_ajax_card_parser,
    ]
    for fn in unit_fns:
        pre_p, pre_f = _passed, _failed
        try:
            fn()
            if _failed - pre_f == 0:
                group_passed += 1
            else:
                group_failed += 1
        except Exception as e:
            import traceback
            print(f"  {FAIL}  EXCEPTION in {fn.__name__}: {e}")
            traceback.print_exc()
            group_failed += 1

    # Group 3: live httpx fetch — capture result for Group 4
    pre_p, pre_f = _passed, _failed
    try:
        sw_articles_cache = test_live_sw_fetch()
        if _failed - pre_f == 0:
            group_passed += 1
        else:
            group_failed += 1
    except Exception as e:
        import traceback
        print(f"  {FAIL}  EXCEPTION in test_live_sw_fetch: {e}")
        traceback.print_exc()
        group_failed += 1

    # Group 4: live API — reuses pre-fetched articles
    pre_p, pre_f = _passed, _failed
    try:
        test_live_gap_check(sw_articles=sw_articles_cache)
        if _failed - pre_f == 0:
            group_passed += 1
        else:
            group_failed += 1
    except Exception as e:
        import traceback
        print(f"  {FAIL}  EXCEPTION in test_live_gap_check: {e}")
        traceback.print_exc()
        group_failed += 1

    print(f"\n{'='*55}")
    print(f"Assertions: {_passed} passed, {_failed} failed")
    print(f"Groups:     {group_passed}/{group_passed + group_failed} passed")
    if _failed == 0:
        print("All tests PASSED ✅")
    print(f"{'='*55}\n")

    shutil.rmtree(_TMPDIR, ignore_errors=True)
    return _failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
