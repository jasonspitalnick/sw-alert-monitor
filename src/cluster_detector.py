"""
src/cluster_detector.py
Saturation threshold detection.

A cluster is "saturated" (ready to trigger a draft) when ALL of:
  1. At least SATURATION_FIRM_COUNT (3) *effective* distinct firms have
     published on the same narrow subject.
  2. Their articles all fall within a SATURATION_WINDOW_DAYS (5) rolling
     window — i.e. there exist N articles from N firms whose date_published
     values span ≤5 days.
  3. The cluster status is still 'active' (not already locked/drafted).

Merged-firm handling
--------------------
Locke Lord and Troutman Pepper merged in Jan 2025.  For counting purposes
they are treated as ONE effective firm.  The MERGED_FIRM_GROUPS list in
config.py defines these pairs; it is easy to extend if more mergers occur.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.config import (
    SATURATION_FIRM_COUNT,
    SATURATION_WINDOW_DAYS,
    MERGED_FIRM_GROUPS,
)
from src import database as db

logger = logging.getLogger(__name__)


# ── Data structure ─────────────────────────────────────────────────────────────

@dataclass
class SaturationResult:
    cluster_id: int
    subject_key: str
    description: str
    effective_firm_count: int
    contributing_firms: list[str]      # actual firm names (may include merged)
    effective_firms: list[str]         # deduplicated after merge normalisation
    earliest_pub_date: Optional[str]   # ISO date string
    latest_pub_date: Optional[str]     # ISO date string
    window_days: int                   # spread between earliest and latest


# ── Merged-firm normalisation ──────────────────────────────────────────────────

def _build_firm_normaliser() -> dict[str, str]:
    """
    Build a lookup: firm_name → canonical_group_name.
    The canonical name is the first member of each merge group.
    """
    normaliser: dict[str, str] = {}
    for group in MERGED_FIRM_GROUPS:
        canonical = group[0]
        for member in group:
            normaliser[member] = canonical
    return normaliser


_FIRM_NORMALISER = _build_firm_normaliser()


def normalise_firm(firm_name: str) -> str:
    """Map a firm name to its canonical name (handles mergers)."""
    return _FIRM_NORMALISER.get(firm_name, firm_name)


def count_effective_firms(firm_names: list[str]) -> tuple[int, list[str]]:
    """
    Given a list of firm names (possibly with merged-firm entries),
    return:
      (effective_count, sorted list of effective/canonical firm names)

    Example:
      ["Locke Lord", "Troutman Pepper", "Gibson Dunn"]
      → (2, ["Locke Lord", "Gibson Dunn"])
    """
    canonical_set: set[str] = {normalise_firm(f) for f in firm_names}
    return len(canonical_set), sorted(canonical_set)


# ── 5-day window check ─────────────────────────────────────────────────────────

def _find_saturating_window(
    firm_dates: list[tuple[str, str]],   # [(firm_name, date_published), ...]
) -> Optional[tuple[str, str, list[str]]]:
    """
    Search for a SATURATION_WINDOW_DAYS span in which ≥ SATURATION_FIRM_COUNT
    effective firms have at least one article.

    Returns (earliest_date, latest_date, list_of_firms) for the smallest
    such window found, or None if no saturating window exists.

    Algorithm: sort by date, then slide a window of width SATURATION_WINDOW_DAYS
    over the sorted dates, checking effective firm count at each position.
    """
    if not firm_dates:
        return None

    # Sort by date; filter out entries with no date
    dated = [
        (normalise_firm(firm), d)
        for firm, d in firm_dates
        if d
    ]
    if not dated:
        return None

    dated.sort(key=lambda x: x[1])

    window_delta = timedelta(days=SATURATION_WINDOW_DAYS)

    for i, (_, start_date_str) in enumerate(dated):
        try:
            start = date.fromisoformat(start_date_str)
        except ValueError:
            continue

        end = start + window_delta
        end_str = end.isoformat()

        firms_in_window: set[str] = set()
        for firm, d in dated[i:]:
            if d > end_str:
                break
            firms_in_window.add(firm)

        if len(firms_in_window) >= SATURATION_FIRM_COUNT:
            # Find the actual date range for this window
            dates_in_window = [
                d for _, d in dated[i:]
                if d <= end_str
            ]
            return (
                min(dates_in_window),
                max(dates_in_window),
                sorted(firms_in_window),
            )

    return None


# ── Per-cluster saturation check ───────────────────────────────────────────────

def check_cluster_saturation(cluster_id: int) -> Optional[SaturationResult]:
    """
    Check whether a single cluster meets the saturation threshold.

    Returns a SaturationResult if saturated, None otherwise.
    Does NOT check lock status — caller is responsible for skipping locked clusters.
    """
    articles = db.get_cluster_articles(cluster_id)
    if not articles:
        return None

    cluster = db.get_cluster_by_key(
        # need subject_key; query by id
        next(
            (r["subject_key"] for r in db.get_all_clusters() if r["id"] == cluster_id),
            None,
        )
    )
    if not cluster:
        return None

    firm_dates: list[tuple[str, str]] = [
        (a["firm_name"], a["date_published"])
        for a in articles
        if a["date_published"]
    ]

    # Also include articles without dates, using date_detected as fallback
    firm_dates_with_fallback: list[tuple[str, str]] = [
        (
            a["firm_name"],
            a["date_published"] or a["date_detected"][:10],  # ISO date from datetime
        )
        for a in articles
    ]

    window = _find_saturating_window(firm_dates_with_fallback)
    if not window:
        return None

    earliest, latest, effective_firms_in_window = window

    try:
        window_days = (
            date.fromisoformat(latest) - date.fromisoformat(earliest)
        ).days
    except ValueError:
        window_days = 0

    all_firm_names = [a["firm_name"] for a in articles]

    return SaturationResult(
        cluster_id=cluster_id,
        subject_key=cluster["subject_key"],
        description=cluster["description"],
        effective_firm_count=len(effective_firms_in_window),
        contributing_firms=sorted(set(all_firm_names)),
        effective_firms=effective_firms_in_window,
        earliest_pub_date=earliest,
        latest_pub_date=latest,
        window_days=window_days,
    )


# ── Scan all active clusters ───────────────────────────────────────────────────

def find_saturated_clusters() -> list[SaturationResult]:
    """
    Check every active (unlocked) cluster and return those that meet the
    saturation threshold.

    Called after each scan's classification pass (Phase 8 scheduler).
    """
    active_clusters = db.get_active_clusters()
    if not active_clusters:
        logger.debug("No active clusters to check.")
        return []

    saturated: list[SaturationResult] = []
    for cluster_row in active_clusters:
        result = check_cluster_saturation(cluster_row["id"])
        if result:
            logger.info(
                "SATURATED  %-50s  firms=%d  window=%d days",
                result.subject_key[:50],
                result.effective_firm_count,
                result.window_days,
            )
            saturated.append(result)

    if not saturated:
        logger.info("No clusters meet saturation threshold yet.")

    return saturated
