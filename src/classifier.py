"""
src/classifier.py
Article classifier — uses the Anthropic API to:
  1. Determine whether a scraped article falls within the monitored practice areas.
  2. If in-scope, assign it to an existing subject cluster or create a new one.
  3. Persist the classification result to the SQLite DB and JSON state files.

Design notes
------------
* Uses the fast/cheap CLASSIFIER_MODEL (claude-haiku-4-5) — called for every
  new article, so cost and speed matter here.
* Provides the full list of existing active clusters to Claude on each call so
  it can semantically match articles to the right cluster rather than creating
  fragmented duplicates.
* subject_key format: snake_case, specific, includes year where applicable
  (e.g. "sec_enforcement_manual_formal_order_2026").
* Retries with exponential back-off on API errors (tenacity).
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import ANTHROPIC_API_KEY, CLASSIFIER_MODEL
from src import database as db
from src import state_manager as sm

logger = logging.getLogger(__name__)

# ── Practice area scope (verbatim from PARAMETERS.md) ─────────────────────────

_PRACTICE_AREAS = """\
- SEC enforcement (Division of Enforcement policy, procedure, and priorities)
- SEC examinations (Division of Examinations priorities and findings)
- Internal investigations (privilege, scope, procedure, best practices)
- White collar criminal defense (DOJ policy, cooperation credit, declinations, DPAs/NPAs)
- FCPA and anti-bribery enforcement
- Whistleblower programs and False Claims Act
- Securities fraud litigation (as it intersects with regulatory enforcement)
- Corporate monitors
- Cybersecurity enforcement (SEC, DOJ)
- Digital assets enforcement (SEC and DOJ actions — not legislative developments)
- Individual accountability in corporate enforcement
- Congressional investigations (as they relate to white collar defense)"""

# ── Narrow-subject guidance (verbatim from PARAMETERS.md) ─────────────────────

_NARROW_SUBJECT_GUIDANCE = """\
TOO BROAD (do not trigger):
- "Recent Trends in SEC Enforcement"
- "Quarterly Regulatory Roundup"
- General mentions of agency activity or leadership changes
- Broad practice area updates without a specific triggering event

PROPERLY NARROW (use as a cluster):
- A specific procedural change with a defined effective date
- A specific court ruling with named parties and a concrete holding
- A precise update to a regulatory manual or guidance document
- A new filing requirement or deadline
- A specific enforcement action establishing a new precedent

When checking if two alerts cover the "same" narrow subject, use semantic
similarity — not title matching. Two articles with different headlines may
address the same specific development. Two articles that both mention the
same agency but focus on different provisions do NOT constitute a cluster."""

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = f"""\
You are a classification assistant for a legal competitive intelligence tool \
that monitors law firm publications for Snell & Wilmer.

Your job: assess whether a law firm article falls within a specific set of \
legal practice areas, and if so, assign it to a narrow subject cluster.

PRACTICE AREAS TO MONITOR:
{_PRACTICE_AREAS}

RULES FOR NARROW SUBJECT CLASSIFICATION:
{_NARROW_SUBJECT_GUIDANCE}

SUBJECT KEY FORMAT: snake_case, specific, year-suffixed where applicable.
Examples: sec_enforcement_manual_formal_order_2026,
          doj_cooperation_credit_policy_2026,
          sec_settlement_simultaneous_waiver_2026

IMPORTANT: Prefer assigning to an EXISTING cluster when there is a reasonable \
semantic match. Only create a NEW cluster if the article addresses a genuinely \
distinct narrow subject not covered by any existing cluster.

RESPOND WITH VALID JSON ONLY — no markdown, no commentary, no code fences."""

# ── Classification prompt template ─────────────────────────────────────────────

def _build_user_prompt(
    firm_name: str,
    title: str,
    url: str,
    active_clusters: list[dict],
) -> str:
    if active_clusters:
        cluster_lines = "\n".join(
            f'  - "{c["subject_key"]}": {c["description"]}'
            for c in active_clusters
        )
        clusters_section = f"EXISTING ACTIVE CLUSTERS:\n{cluster_lines}"
    else:
        clusters_section = "EXISTING ACTIVE CLUSTERS: (none yet)"

    return f"""\
{clusters_section}

ARTICLE TO CLASSIFY:
  Firm:  {firm_name}
  Title: {title}
  URL:   {url}

INSTRUCTIONS:
1. Is this article in-scope for the practice areas listed above?
2. If in-scope, does it address a PROPERLY NARROW subject (not a broad roundup)?
3. If narrow and in-scope:
   a. Does it match an existing cluster? → return that subject_key.
   b. If genuinely new → invent a new snake_case subject_key and description.

RESPOND WITH JSON:
{{
  "is_in_scope": true | false,
  "is_narrow": true | false,
  "subject_key": "<key>" | null,
  "subject_description": "<one-sentence description of the specific narrow development>" | null,
  "is_new_cluster": true | false,
  "reasoning": "<1–2 sentences>"
}}"""


# ── Data structure ─────────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    article_id: int
    firm_name: str
    is_in_scope: bool
    subject_key: Optional[str]
    subject_description: Optional[str]
    is_new_cluster: bool
    cluster_id: Optional[int]
    reasoning: Optional[str]


# ── API call ───────────────────────────────────────────────────────────────────

_client: Optional[anthropic.Anthropic] = None

def _get_client() -> anthropic.Anthropic:
    """
    Return (or lazily create) the shared Anthropic client.
    Read the key at call time rather than import time so that tests can set
    the API key via dotenv after the module is first imported.
    """
    global _client
    if _client is None:
        import src.config as _live_cfg  # re-import to pick up any runtime patches
        key = _live_cfg.ANTHROPIC_API_KEY or ANTHROPIC_API_KEY
        _client = anthropic.Anthropic(api_key=key)
    return _client


@retry(
    retry=retry_if_exception_type((anthropic.APIConnectionError, anthropic.RateLimitError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
)
def _call_classifier_api(user_prompt: str) -> dict:
    """
    Call the Anthropic API with the classification prompt.
    Returns the parsed JSON response dict.
    Raises ValueError on unparseable response.
    """
    client = _get_client()
    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = response.content[0].text.strip()

    # Strip accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Unparseable classifier response: {raw[:300]}") from exc


# ── Subject-key sanitisation ───────────────────────────────────────────────────

def _sanitise_key(raw: str) -> str:
    """
    Normalise a subject_key to safe snake_case.
    Strips leading/trailing underscores and collapses runs of underscores.
    """
    key = raw.lower().strip()
    key = re.sub(r"[^a-z0-9_]", "_", key)
    key = re.sub(r"_+", "_", key)
    return key.strip("_")[:120]


# ── Single-article classification ─────────────────────────────────────────────

def classify_article(article_id: int) -> Optional[ClassificationResult]:
    """
    Classify one article by ID.
    - Updates articles.classified_at and articles.is_in_scope in the DB.
    - If in-scope and narrow, upserts the cluster and links the article.
    - Updates alert_tracker.json via state_manager.
    Returns a ClassificationResult or None if the article doesn't exist.
    """
    article = db.get_article_by_id(article_id)
    if not article:
        logger.warning("classify_article: article_id=%d not found", article_id)
        return None

    # Build context: load all active clusters for semantic matching
    active_rows = db.get_active_clusters()
    active_clusters = [
        {"subject_key": r["subject_key"], "description": r["description"]}
        for r in active_rows
    ]

    user_prompt = _build_user_prompt(
        firm_name=article["firm_name"],
        title=article["title"],
        url=article["url"],
        active_clusters=active_clusters,
    )

    try:
        parsed = _call_classifier_api(user_prompt)
    except Exception as exc:
        logger.error(
            "Classifier API error for article %d (%s): %s",
            article_id, article["title"][:60], exc,
        )
        # Mark as classified (out of scope) so we don't retry endlessly
        db.mark_article_classified(article_id, is_in_scope=False)
        return ClassificationResult(
            article_id=article_id,
            firm_name=article["firm_name"],
            is_in_scope=False,
            subject_key=None,
            subject_description=None,
            is_new_cluster=False,
            cluster_id=None,
            reasoning=f"API error: {exc}",
        )

    is_in_scope: bool = bool(parsed.get("is_in_scope", False))
    is_narrow: bool = bool(parsed.get("is_narrow", False))
    raw_key: Optional[str] = parsed.get("subject_key")
    description: Optional[str] = parsed.get("subject_description")
    is_new_cluster: bool = bool(parsed.get("is_new_cluster", False))
    reasoning: Optional[str] = parsed.get("reasoning")

    # Only proceed to cluster assignment if in-scope AND narrow
    effective_in_scope = is_in_scope and is_narrow

    cluster_id: Optional[int] = None
    subject_key: Optional[str] = None

    if effective_in_scope and raw_key:
        subject_key = _sanitise_key(raw_key)
        cluster_id = db.upsert_cluster(
            subject_key=subject_key,
            description=description or subject_key.replace("_", " ").title(),
        )
        db.link_article_to_cluster(cluster_id, article_id)
        db.refresh_cluster_stats(cluster_id)

        # Mirror into alert_tracker.json
        sm.tracker_add_article(
            subject_key=subject_key,
            description=description or subject_key.replace("_", " ").title(),
            firm_name=article["firm_name"],
            title=article["title"],
            url=article["url"],
            date_detected=article["date_detected"],
            date_published=article["date_published"],
        )

        logger.info(
            "%-42s  cluster=%-50s  new=%s",
            article["firm_name"][:42],
            subject_key[:50],
            is_new_cluster,
        )
    else:
        logger.debug(
            "Out of scope / too broad: [%s] %s",
            article["firm_name"], article["title"][:70],
        )

    db.mark_article_classified(article_id, is_in_scope=effective_in_scope)

    return ClassificationResult(
        article_id=article_id,
        firm_name=article["firm_name"],
        is_in_scope=effective_in_scope,
        subject_key=subject_key,
        subject_description=description,
        is_new_cluster=is_new_cluster,
        cluster_id=cluster_id,
        reasoning=reasoning,
    )


# ── Bulk classification ────────────────────────────────────────────────────────

def classify_new_articles(batch_limit: int = 200) -> list[ClassificationResult]:
    """
    Classify all articles that haven't been processed yet (classified_at IS NULL).
    Processes up to `batch_limit` articles per call (protects against runaway
    API spend on the first scan which may surface thousands of articles).
    Returns a list of ClassificationResult objects.
    """
    unclassified = db.get_unclassified_articles(limit=batch_limit)
    if not unclassified:
        logger.info("No unclassified articles to process.")
        return []

    logger.info("Classifying %d new articles...", len(unclassified))
    results: list[ClassificationResult] = []

    for article in unclassified:
        result = classify_article(article["id"])
        if result:
            results.append(result)

    in_scope = sum(1 for r in results if r.is_in_scope)
    new_clusters = sum(1 for r in results if r.is_new_cluster)
    logger.info(
        "Classification complete: %d processed, %d in-scope, %d new clusters",
        len(results), in_scope, new_clusters,
    )
    return results
