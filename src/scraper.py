"""
src/scraper.py
Playwright-based scraper for law firm publications pages.

For each firm:
  1. Load the publications URL in headless Chromium.
  2. Wait for JS content to render (domcontentloaded + 2 s scroll pass).
  3. Extract article links + publication dates via BeautifulSoup heuristics.
  4. Save new articles to the SQLite DB (all firms except S&W).
  5. Log the scrape result (success / failure + article count).

Graceful failure: exceptions per firm are caught, logged, and the scan
continues with the next firm.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urljoin

from playwright.async_api import (
    async_playwright,
    Browser,
    TimeoutError as PlaywrightTimeout,
)
from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateutil_parser
from dateutil.parser import ParserError

from src.config import (
    FIRMS_FILE,
    PAGE_TIMEOUT_MS,
    MAX_ARTICLES_PER_FIRM,
    SW_FIRM_NAME,
)
from src import database as db

logger = logging.getLogger(__name__)

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class ScrapedArticle:
    firm_name: str
    title: str
    url: str
    date_published: Optional[str]   # ISO date string YYYY-MM-DD, or None
    full_text: Optional[str] = None


@dataclass
class FirmScrapeResult:
    firm_name: str
    url: str
    success: bool
    articles: list[ScrapedArticle] = field(default_factory=list)
    new_count: int = 0
    error: Optional[str] = None


# ── Date extraction ────────────────────────────────────────────────────────────

# Matches the most common date formats on law firm sites
_DATE_RE = re.compile(
    r'\b(?:'
    # "March 14, 2026" / "Mar 14 2026" / "March 2026"
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'[\s.]+\d{1,2},?\s+\d{4}'
    r'|'
    # "14 March 2026"
    r'\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+\d{4}'
    r'|'
    # ISO: "2026-03-14"
    r'\d{4}-\d{2}-\d{2}'
    r')\b',
    re.IGNORECASE,
)

# Matches date-in-URL like /2026/03/14/ or /2026/03/
_URL_DATE_RE = re.compile(r'/(\d{4})/(\d{2})(?:/(\d{2}))?')

_MIN_YEAR = 2015


def _parse_date_str(text: str) -> Optional[str]:
    """Try to parse a date string; return ISO YYYY-MM-DD or None."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        dt = dateutil_parser.parse(m.group(0), default=datetime(2000, 1, 1))
        if _MIN_YEAR <= dt.year <= datetime.now(timezone.utc).year + 5:
            return dt.strftime("%Y-%m-%d")
    except (ParserError, OverflowError, ValueError):
        pass
    return None


def _date_from_url(url: str) -> Optional[str]:
    """Extract a date from URL path segments like /2026/03/14/."""
    m = _URL_DATE_RE.search(url)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if m.group(3) else 1
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _find_date_near_tag(tag: Tag, depth: int = 5) -> Optional[str]:
    """
    Walk up the DOM from `tag` up to `depth` levels, searching for a date.
    Checks <time datetime="..."> attributes first, then plain text patterns
    in short child elements (date spans are typically short).
    """
    node: Tag = tag
    for _ in range(depth):
        parent = getattr(node, "parent", None)
        if parent is None:
            break

        # Explicit <time> elements
        for time_tag in parent.find_all("time", limit=5):
            dt_attr = time_tag.get("datetime", "")
            if dt_attr:
                parsed = _parse_date_str(dt_attr)
                if parsed:
                    return parsed
            parsed = _parse_date_str(time_tag.get_text(" "))
            if parsed:
                return parsed

        # Short direct-child text nodes often contain dates
        for child in parent.children:
            if hasattr(child, "get_text"):
                child_text = child.get_text(" ", strip=True)
                if child_text and len(child_text) < 80:
                    parsed = _parse_date_str(child_text)
                    if parsed:
                        return parsed

        node = parent
    return None


# ── URL filtering ──────────────────────────────────────────────────────────────

_SKIP_LAST_SEGMENTS = frozenset({
    "all", "search", "filter", "tag", "tags", "category", "categories",
    "author", "authors", "people", "person", "contact", "about",
    "careers", "events", "login", "logout", "sitemap", "privacy",
    "terms", "disclaimer", "subscribe", "newsletters", "rss",
    "podcast", "podcasts", "video", "videos", "webinar", "webinars",
    "publications", "insights", "news", "resources", "alerts",
    "perspectives", "knowledge", "thought-leadership", "topics",
    "practices", "industries", "capabilities",
})

_SKIP_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js",
    ".mp4", ".mp3", ".wav",
})

_SKIP_PATH_KEYWORDS = frozenset({
    "/people/", "/attorneys/", "/lawyers/", "/professionals/",
    "/careers/", "/about/", "/contact/", "/events/",
    "/login/", "/search/", "/subscribe/",
})


def _is_article_url(href: str, base_parsed) -> bool:
    """
    Return True if href plausibly leads to an individual article page
    rather than a listing, navigation, or file download.
    """
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False

    parsed = urlparse(href)

    # Must be same domain (or relative — empty netloc)
    if parsed.netloc:
        norm_href = parsed.netloc.lower().lstrip("www.")
        norm_base = base_parsed.netloc.lower().lstrip("www.")
        if norm_href != norm_base:
            return False

    path = parsed.path.rstrip("/")
    if not path:
        return False

    path_lower = path.lower()

    for kw in _SKIP_PATH_KEYWORDS:
        if kw in path_lower:
            return False

    for ext in _SKIP_EXTENSIONS:
        if path_lower.endswith(ext):
            return False

    segments = [s for s in path.split("/") if s]
    if not segments:
        return False

    last_seg = segments[-1].lower()

    if len(last_seg) < 5:
        return False

    if last_seg in _SKIP_LAST_SEGMENTS:
        return False

    # For single-segment URLs (articles at root domain), require the slug
    # to look like an article title: either long or hyphen-rich.
    # This handles firms like Gibson Dunn: /sec-enforcement-update-march-2026/
    if len(segments) == 1:
        if len(last_seg) < 20 and last_seg.count("-") < 2:
            return False

    return True


# ── Navigation / chrome detection ─────────────────────────────────────────────

_NAV_TAG_NAMES = frozenset({"nav", "header", "footer", "aside"})
_NAV_CLASS_KEYWORDS = (
    "navbar", "nav-", "-nav", "site-header", "site-footer",
    "footer", "header", "sidebar", "cookie", "banner",
    "breadcrumb", "pagination", "pager",
)


def _is_in_chrome(tag: Tag) -> bool:
    """Return True if the tag lives inside a nav / header / footer region."""
    for parent in tag.parents:
        name = getattr(parent, "name", None)
        if name in _NAV_TAG_NAMES:
            return True
        if hasattr(parent, "get"):
            cls_str = " ".join(parent.get("class", [])).lower()
            if any(kw in cls_str for kw in _NAV_CLASS_KEYWORDS):
                return True
    return False


# ── Core HTML extraction ───────────────────────────────────────────────────────

_SKIP_TITLES = frozenset({
    "read more", "learn more", "view more", "see all", "show more",
    "subscribe", "download", "watch now", "listen now", "view all",
    "all insights", "all publications", "load more",
})


def extract_articles_from_html(
    html: str,
    page_url: str,
    firm_name: str,
) -> list[ScrapedArticle]:
    """
    Parse `html` and return up to MAX_ARTICLES_PER_FIRM ScrapedArticle objects.

    Strategy:
      1. Parse with lxml.
      2. Walk every <a href> on the page.
      3. Reject nav/chrome links, non-article URLs, and bad title lengths.
      4. For surviving candidates, find the nearest publication date.
      5. Deduplicate by normalised URL.
    """
    soup = BeautifulSoup(html, "lxml")
    base_parsed = urlparse(page_url)
    base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
    page_path = base_parsed.path.rstrip("/")

    seen_urls: set[str] = set()
    results: list[ScrapedArticle] = []

    for a_tag in soup.find_all("a", href=True):
        if len(results) >= MAX_ARTICLES_PER_FIRM:
            break

        href: str = a_tag["href"].strip()

        # Normalise relative URLs
        if href.startswith("//"):
            href = base_parsed.scheme + ":" + href
        elif href.startswith("/"):
            href = base_origin + href
        elif not href.startswith("http"):
            href = urljoin(page_url, href)

        # Strip query string / fragment for comparison and storage
        parsed_href = urlparse(href)
        clean_href = (
            f"{parsed_href.scheme}://{parsed_href.netloc}{parsed_href.path}"
        )

        # Don't link back to the listing page itself
        if parsed_href.path.rstrip("/") == page_path:
            continue

        if not _is_article_url(clean_href, base_parsed):
            continue

        if clean_href in seen_urls:
            continue

        if _is_in_chrome(a_tag):
            continue

        title = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True)).strip()

        if len(title) < 15 or len(title) > 300:
            continue

        if title.lower() in _SKIP_TITLES:
            continue

        date_pub = (
            _find_date_near_tag(a_tag, depth=5)
            or _date_from_url(clean_href)
        )

        seen_urls.add(clean_href)
        results.append(
            ScrapedArticle(
                firm_name=firm_name,
                title=title,
                url=clean_href,
                date_published=date_pub,
            )
        )

    return results


# ── Playwright page loading ────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

# Media file pattern — abort these requests to speed up page loads
_MEDIA_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|woff2?|ttf|eot|mp4|webm)(\?.*)?$", re.I
)


async def _load_page_html(browser: Browser, url: str) -> str:
    """
    Open `url` in a fresh browser context, wait for content to render,
    return the outer HTML.  Context is always closed on exit.
    """
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1280, "height": 900},
        java_script_enabled=True,
        ignore_https_errors=True,
    )
    page = await context.new_page()

    # Abort heavy media to speed things up
    await page.route(
        _MEDIA_RE,
        lambda route, _: route.abort(),
    )

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

        # For JS-heavy SPAs, try to wait for network to settle (up to 5 s).
        # This is especially important for React/API-driven publication lists.
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeout:
            pass  # best-effort; proceed with whatever rendered so far

        # Allow additional JS rendering time
        await asyncio.sleep(1.5)

        # Scroll 40% down to trigger lazy-loaded lists
        await page.evaluate(
            "window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.4))"
        )
        await asyncio.sleep(0.5)

        return await page.content()
    finally:
        await context.close()


# ── Per-firm scrape ────────────────────────────────────────────────────────────

async def scrape_firm(
    browser: Browser,
    firm_name: str,
    url: str,
) -> FirmScrapeResult:
    """
    Scrape one firm, persist new articles to the DB, log the outcome.
    Never raises — all exceptions produce a failure FirmScrapeResult.
    """
    try:
        html = await _load_page_html(browser, url)
        articles = extract_articles_from_html(html, url, firm_name)

        new_count = 0
        for art in articles:
            if not db.article_exists(art.url):
                db.upsert_article(
                    firm_name=art.firm_name,
                    title=art.title,
                    url=art.url,
                    date_published=art.date_published,
                    is_in_scope=False,   # Phase 4 classifier will update this
                    full_text=art.full_text,
                )
                new_count += 1

        db.log_scrape(firm_name, url, True, len(articles))
        logger.info(
            "%-42s  found=%3d  new=%3d",
            firm_name[:42], len(articles), new_count,
        )
        return FirmScrapeResult(
            firm_name=firm_name,
            url=url,
            success=True,
            articles=articles,
            new_count=new_count,
        )

    except PlaywrightTimeout:
        msg = f"Timed out after {PAGE_TIMEOUT_MS // 1000}s"
        db.log_scrape(firm_name, url, False, 0, msg)
        logger.warning("TIMEOUT  %-42s", firm_name[:42])
        return FirmScrapeResult(firm_name, url, False, error=msg)

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        db.log_scrape(firm_name, url, False, 0, msg)
        logger.warning("ERROR    %-42s  %s", firm_name[:42], msg[:120])
        return FirmScrapeResult(firm_name, url, False, error=msg)


# ── Full-scan entry points ─────────────────────────────────────────────────────

def load_firms(include_sw: bool = False) -> list[dict]:
    """
    Load law_firm_alerts.json.
    Skips entries with null URLs.  Skips S&W unless include_sw=True.
    """
    with open(FIRMS_FILE, "r", encoding="utf-8") as f:
        all_firms = json.load(f)
    return [
        firm for firm in all_firms
        if firm.get("url") and (include_sw or firm["name"] != SW_FIRM_NAME)
    ]


async def _run_scrape_async(
    firms: list[dict],
    inter_firm_delay: float = 0.5,
) -> list[FirmScrapeResult]:
    """Scrape firms sequentially, reusing a single Chromium instance."""
    db.init_db()
    results: list[FirmScrapeResult] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            # --no-sandbox is required when running as root in Docker/cloud.
            # --disable-dev-shm-usage prevents crashes caused by the small
            # /dev/shm shared-memory partition in container environments.
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            for firm in firms:
                result = await scrape_firm(browser, firm["name"], firm["url"])
                results.append(result)
                if inter_firm_delay > 0:
                    await asyncio.sleep(inter_firm_delay)
        finally:
            await browser.close()

    return results


def run_scrape(firms: Optional[list[dict]] = None) -> list[FirmScrapeResult]:
    """
    Synchronous public entry point — called by the scheduler (Phase 8).
    Scrapes all competitor firms (or a supplied subset).
    Returns a list of FirmScrapeResult objects.
    """
    if firms is None:
        firms = load_firms()
    return asyncio.run(_run_scrape_async(firms))
