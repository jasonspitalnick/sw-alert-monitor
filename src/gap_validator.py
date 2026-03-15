"""
src/gap_validator.py
Snell & Wilmer gap-validation step (Phase 5).

Checks whether S&W has already published a *dedicated* client alert on the
triggering subject.  If they have, the drafting trigger is suppressed.

Definition from PARAMETERS.md
------------------------------
A "dedicated alert" means a standalone article or post focused primarily on
that topic.  A passing mention, a single bullet point in a roundup, or a
tangential reference does NOT satisfy the gap condition — the drafting trigger
still fires.

Implementation notes
--------------------
The S&W publications page (swlaw.com/publications/?all) uses a WordPress AJAX
endpoint (wp-admin/admin-ajax.php) to load article listings.  Rather than
using Playwright (which is defeated by the cookie consent overlay on this
site), we:

  1. Fetch the publications page HTML with httpx to extract the WP nonce.
  2. POST directly to wp-admin/admin-ajax.php with the nonce to retrieve
     paginated article cards (10 per page).
  3. Collect up to _MAX_PAGES pages (= _MAX_PAGES * 10 articles).
  4. Pass the article titles to Claude (claude-haiku-4-5) to check coverage.

Claude prompt design
--------------------
Claude is asked: does any article in the list constitute a *dedicated* alert
primarily focused on the triggering subject?  It errs toward has_gap=True
(trigger the draft) when in doubt, matching PARAMETERS.md rule that only a
dedicated alert suppresses drafting.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone

import httpx
import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from bs4 import BeautifulSoup

from src.config import ANTHROPIC_API_KEY, CLASSIFIER_MODEL, SW_PUBLICATIONS_URL

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Number of paginated AJAX pages to fetch (10 articles each)
_MAX_PAGES: int = 10   # = 100 articles

# How many S&W articles to pass to Claude in the gap-check prompt
_MAX_ARTICLES_FOR_CLAUDE: int = 100

# HTTP timeouts (seconds)
_CONNECT_TIMEOUT: float = 10.0
_READ_TIMEOUT: float     = 20.0

# WordPress AJAX endpoint (same host as SW_PUBLICATIONS_URL)
_AJAX_URL: str = "https://www.swlaw.com/wp-admin/admin-ajax.php"

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class SWArticle:
    title: str
    url: str
    date_published: Optional[str] = None   # ISO YYYY-MM-DD or None


@dataclass
class GapValidationResult:
    subject_key: str
    has_gap: bool                          # True → proceed; False → suppress
    sw_articles_checked: int               # number of S&W articles evaluated
    blocking_article: Optional[SWArticle]  # S&W article that blocks (if any)
    reasoning: str


# ── S&W article fetcher (httpx, no Playwright) ─────────────────────────────────

def _extract_nonce(html: str) -> Optional[str]:
    """
    Extract the WordPress nonce from the hidden form field on the page.
    The publications listing form embeds:
      <input type="hidden" id="_wpnonce" name="_wpnonce" value="<hex>">
    """
    m = re.search(r'name="_wpnonce"\s+value="([a-f0-9]+)"', html)
    return m.group(1) if m else None


def _parse_ajax_cards(content_html: str) -> list[SWArticle]:
    """
    Parse the HTML fragment returned by the AJAX endpoint.
    Each article is a <div class="card-news-list"> containing:
      - <div class="date">Mar 09, 2026</div>
      - <a class="card-news-list__title" href="..."><h3>Title</h3></a>

    External URLs (press/media mentions hosted on third-party sites) are
    excluded — the gap check only cares about S&W-authored publications.
    """
    soup = BeautifulSoup(content_html, "lxml")
    articles: list[SWArticle] = []

    for card in soup.find_all("div", class_="card-news-list"):
        link = card.find("a", class_="card-news-list__title")
        date_div = card.find("div", class_="date")
        if not link:
            continue

        title = link.get_text(strip=True)
        url = link.get("href", "").strip()
        if not title or not url:
            continue

        # Skip external media mentions (press coverage hosted off swlaw.com)
        if url.startswith("http") and "swlaw.com" not in url:
            continue

        # Parse date string like "Mar 09, 2026" → "2026-03-09"
        date_str = date_div.get_text(strip=True) if date_div else None
        iso_date: Optional[str] = None
        if date_str:
            try:
                from dateutil import parser as dateutil_parser
                dt = dateutil_parser.parse(date_str)
                iso_date = dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        articles.append(SWArticle(title=title, url=url, date_published=iso_date))

    return articles


def fetch_sw_articles() -> list[SWArticle]:
    """
    Fetch recent S&W publications using the WordPress AJAX endpoint.

    Returns a list of SWArticle objects sorted newest-first.
    Returns an empty list on any unrecoverable error.
    """
    client = httpx.Client(
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
        timeout=httpx.Timeout(_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
    )
    all_articles: list[SWArticle] = []

    try:
        # Step 1: fetch the page HTML to extract the nonce
        resp = client.get(SW_PUBLICATIONS_URL)
        resp.raise_for_status()
        nonce = _extract_nonce(resp.text)

        if not nonce:
            logger.error(
                "No WordPress nonce found on S&W publications page — "
                "cannot fetch articles"
            )
            return []

        logger.debug("Extracted S&W nonce: %s", nonce)

        # Step 2: paginate through AJAX results
        for page_num in range(1, _MAX_PAGES + 1):
            ajax_resp = client.post(
                _AJAX_URL,
                data={
                    "action":           "load_listing_results",
                    "_wpnonce":         nonce,
                    "_wp_http_referer": "/publications/?all",
                    "page":             str(page_num),
                    "perpage":          "10",
                    "post_types":       "publications",
                    "orderby":          "date",
                    "order":            "DESC",
                    "template":         "components/card-news-list/component",
                    "search":           "",
                    "pager":            "pagenavi",
                    "filters":          "",
                },
                headers={
                    "Referer":           SW_PUBLICATIONS_URL,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            ajax_resp.raise_for_status()

            payload = ajax_resp.json()
            if not payload.get("success"):
                logger.warning(
                    "S&W AJAX page %d returned success=false — stopping", page_num
                )
                break

            content_html: str = payload.get("data", {}).get("content", "")
            cards = _parse_ajax_cards(content_html)

            if not cards:
                logger.debug(
                    "No cards on S&W AJAX page %d — end of results", page_num
                )
                break

            all_articles.extend(cards)
            logger.debug("S&W AJAX page %d: %d cards", page_num, len(cards))

    except httpx.HTTPError as exc:
        logger.error("HTTP error fetching S&W publications: %s", exc)
    except Exception as exc:
        logger.error("Unexpected error fetching S&W publications: %s", exc)
    finally:
        client.close()

    logger.info("Fetched %d S&W articles total", len(all_articles))
    return all_articles


# ── Claude gap-check ───────────────────────────────────────────────────────────

_GAP_SYSTEM_PROMPT = """\
You are a gap-validation assistant for a legal competitive intelligence tool \
that monitors law firm publications for Snell & Wilmer.

Your job: determine whether Snell & Wilmer has already published a DEDICATED \
CLIENT ALERT on a specific narrow legal development.

DEFINITION OF "DEDICATED ALERT":
A standalone article or post whose PRIMARY FOCUS is the triggering development. \
This includes pieces that lead with or extensively analyse the specific event, \
ruling, or procedural change.

NOT a dedicated alert (gap condition is still met — draft should proceed):
- A passing mention in a quarterly roundup or multi-topic update
- A single bullet point in a practice-area overview
- A tangential reference in an article primarily about something else
- Any general survey article that merely notes the development in passing

KEY RULE: When in doubt, err toward has_dedicated_alert=false (favour \
triggering the draft).  Only return has_dedicated_alert=true when you are \
confident a listed article is PRIMARILY about the triggering subject.

RESPOND WITH VALID JSON ONLY — no markdown, no commentary, no code fences."""


def _build_gap_prompt(
    subject_key: str,
    subject_description: str,
    sw_articles: list[SWArticle],
) -> str:
    article_lines = "\n".join(
        f"  {i + 1}. [{a.date_published or 'n/a'}] {a.title}"
        for i, a in enumerate(sw_articles)
    )
    return f"""\
TRIGGERING SUBJECT:
  Key:         {subject_key}
  Description: {subject_description}

SNELL & WILMER RECENT PUBLICATIONS (titles only):
{article_lines}

TASK:
Review the list above.  Is there any article that constitutes a DEDICATED \
CLIENT ALERT primarily focused on the triggering subject described above?

Remember: roundups, quarterly reviews, or articles that merely mention the \
topic in passing do NOT count.  Only a standalone piece focused primarily on \
this specific narrow development qualifies.

RESPOND WITH JSON:
{{
  "has_dedicated_alert": true | false,
  "blocking_article_index": <1-based integer> | null,
  "blocking_article_title": "<title>" | null,
  "reasoning": "<1–2 sentences explaining your conclusion>"
}}"""


# ── Anthropic client (lazy, mirrors classifier.py) ─────────────────────────────

_gap_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    """
    Lazily create the shared Anthropic client.
    Reads the key at call time so tests that patch src.config after import work.
    """
    global _gap_client
    if _gap_client is None:
        import src.config as _live_cfg
        key = _live_cfg.ANTHROPIC_API_KEY or ANTHROPIC_API_KEY
        _gap_client = anthropic.Anthropic(api_key=key)
    return _gap_client


@retry(
    retry=retry_if_exception_type(
        (anthropic.APIConnectionError, anthropic.RateLimitError)
    ),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
)
def _call_gap_api(user_prompt: str) -> dict:
    """
    Call the Anthropic API for gap validation.
    Returns the parsed JSON response dict.
    Raises ValueError on unparseable response.
    """
    client = _get_client()
    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=512,
        system=_GAP_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = response.content[0].text.strip()

    # Strip accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Unparseable gap-check response: {raw[:300]}") from exc


# ── Public entry point ─────────────────────────────────────────────────────────

def validate_gap(
    subject_key: str,
    subject_description: str,
    sw_articles: Optional[list[SWArticle]] = None,
) -> GapValidationResult:
    """
    Check whether S&W has a dedicated alert on the given subject.

    Parameters
    ----------
    subject_key : str
        The snake_case subject cluster key (e.g. "sec_enforcement_manual_2026").
    subject_description : str
        Human-readable description of the specific narrow development.
    sw_articles : list[SWArticle] | None
        Pre-fetched S&W articles.  Pass this in tests to skip the live fetch.
        If None, articles are fetched automatically via the AJAX endpoint.

    Returns
    -------
    GapValidationResult
        has_gap=True  → S&W has NOT covered it → proceed with drafting
        has_gap=False → S&W already covered it → suppress drafting
    """
    # ── Step 1: obtain S&W article list ───────────────────────────────────────
    if sw_articles is None:
        sw_articles = fetch_sw_articles()

    if not sw_articles:
        logger.warning(
            "No S&W articles available for gap check on '%s' — "
            "defaulting to has_gap=True (proceed with draft)",
            subject_key,
        )
        return GapValidationResult(
            subject_key=subject_key,
            has_gap=True,
            sw_articles_checked=0,
            blocking_article=None,
            reasoning=(
                "No S&W articles were available for comparison — "
                "assuming gap exists and proceeding with draft."
            ),
        )

    # ── Step 2: sort by date desc and trim to budget ───────────────────────────
    def _sort_key(a: SWArticle) -> str:
        return a.date_published or "0000-00-00"

    articles_to_check = sorted(sw_articles, key=_sort_key, reverse=True)
    articles_to_check = articles_to_check[:_MAX_ARTICLES_FOR_CLAUDE]

    # ── Step 3: ask Claude ─────────────────────────────────────────────────────
    user_prompt = _build_gap_prompt(subject_key, subject_description, articles_to_check)

    try:
        parsed = _call_gap_api(user_prompt)
    except Exception as exc:
        logger.error(
            "Gap-check API error for '%s': %s — defaulting to has_gap=True",
            subject_key, exc,
        )
        return GapValidationResult(
            subject_key=subject_key,
            has_gap=True,
            sw_articles_checked=len(articles_to_check),
            blocking_article=None,
            reasoning=f"API error: {exc} — assuming gap exists.",
        )

    # ── Step 4: parse response ─────────────────────────────────────────────────
    has_dedicated: bool = bool(parsed.get("has_dedicated_alert", False))
    idx: Optional[int]   = parsed.get("blocking_article_index")   # 1-based
    blocking_title: Optional[str] = parsed.get("blocking_article_title")
    reasoning: str = parsed.get("reasoning", "")

    blocking: Optional[SWArticle] = None
    if has_dedicated:
        # Resolve by index first, then by title
        if idx and isinstance(idx, int) and 1 <= idx <= len(articles_to_check):
            blocking = articles_to_check[idx - 1]
        elif blocking_title:
            blocking = next(
                (a for a in articles_to_check if a.title == blocking_title),
                None,
            )

    has_gap = not has_dedicated

    logger.info(
        "Gap validation  subject=%-40s  has_gap=%s  reason=%s",
        subject_key[:40], has_gap, reasoning[:120],
    )

    return GapValidationResult(
        subject_key=subject_key,
        has_gap=has_gap,
        sw_articles_checked=len(articles_to_check),
        blocking_article=blocking,
        reasoning=reasoning,
    )
