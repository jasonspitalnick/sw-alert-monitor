"""
src/state_manager.py
Manages the two JSON state files:

  alert_tracker.json  — human-readable snapshot of all clusters and their
                         articles; kept in sync with the SQLite DB after
                         each scan so a human can inspect current state
                         without running SQL.

  drafted_topics.json — lightweight lock file: a dict of subject_key →
                         lock metadata.  Checked before every draft trigger.
                         Written atomically so it survives partial crashes.

Design note: The SQLite DB is the authoritative source for all queries.
These JSON files serve as (a) a human-readable audit trail and (b) an
additional safety net for the drafted-topics lock so it can be read without
importing any DB code.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.config import ALERT_TRACKER_FILE, DRAFTED_TOPICS_FILE


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: write to temp file, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".tmp_", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)   # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_json(path: Path, default: Any) -> Any:
    """Load JSON from path; return default if missing or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ── alert_tracker.json ─────────────────────────────────────────────────────────
#
# Schema:
# {
#   "last_updated": "2026-03-14T07:00:00Z",
#   "clusters": {
#     "<subject_key>": {
#       "subject_key": str,
#       "description": str,
#       "status": "active" | "locked",
#       "firm_count": int,
#       "earliest_pub_date": str | null,
#       "latest_pub_date":   str | null,
#       "locked_at":         str | null,
#       "draft_file":        str | null,
#       "created_at":        str,
#       "updated_at":        str,
#       "articles": [
#         {
#           "firm_name": str,
#           "title": str,
#           "url": str,
#           "date_detected": str,
#           "date_published": str | null
#         }, ...
#       ]
#     }
#   }
# }

def load_tracker() -> dict:
    """Load alert_tracker.json; return empty structure if missing."""
    return _load_json(
        ALERT_TRACKER_FILE,
        {"last_updated": _now_iso(), "clusters": {}},
    )


def save_tracker(tracker: dict) -> None:
    tracker["last_updated"] = _now_iso()
    _atomic_write(ALERT_TRACKER_FILE, tracker)


def tracker_add_article(
    subject_key: str,
    description: str,
    firm_name: str,
    title: str,
    url: str,
    date_detected: str,
    date_published: Optional[str],
) -> None:
    """
    Add an article to the named cluster in alert_tracker.json.
    Creates the cluster entry if it doesn't exist.
    """
    tracker = load_tracker()
    clusters = tracker.setdefault("clusters", {})

    if subject_key not in clusters:
        clusters[subject_key] = {
            "subject_key": subject_key,
            "description": description,
            "status": "active",
            "firm_count": 0,
            "earliest_pub_date": None,
            "latest_pub_date": None,
            "locked_at": None,
            "draft_file": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "articles": [],
        }

    cluster = clusters[subject_key]

    # Deduplicate by URL
    existing_urls = {a["url"] for a in cluster["articles"]}
    if url not in existing_urls:
        cluster["articles"].append(
            {
                "firm_name": firm_name,
                "title": title,
                "url": url,
                "date_detected": date_detected,
                "date_published": date_published,
            }
        )

    # Recompute derived stats
    cluster["firm_count"] = len(
        {a["firm_name"] for a in cluster["articles"]}
    )
    pub_dates = [
        a["date_published"]
        for a in cluster["articles"]
        if a["date_published"]
    ]
    if pub_dates:
        cluster["earliest_pub_date"] = min(pub_dates)
        cluster["latest_pub_date"] = max(pub_dates)
    cluster["description"] = description   # allow description updates
    cluster["updated_at"] = _now_iso()

    save_tracker(tracker)


def tracker_lock_subject(
    subject_key: str,
    draft_file: str,
) -> None:
    """Mark a subject cluster as locked in alert_tracker.json."""
    tracker = load_tracker()
    cluster = tracker.get("clusters", {}).get(subject_key)
    if cluster:
        cluster["status"] = "locked"
        cluster["locked_at"] = _now_iso()
        cluster["draft_file"] = draft_file
        cluster["updated_at"] = _now_iso()
        save_tracker(tracker)


def tracker_get_cluster(subject_key: str) -> Optional[dict]:
    """Return a cluster dict from the tracker, or None."""
    tracker = load_tracker()
    return tracker.get("clusters", {}).get(subject_key)


def tracker_all_clusters() -> list[dict]:
    """Return all cluster dicts sorted newest-first."""
    tracker = load_tracker()
    return sorted(
        tracker.get("clusters", {}).values(),
        key=lambda c: c.get("updated_at", ""),
        reverse=True,
    )


# ── drafted_topics.json ────────────────────────────────────────────────────────
#
# Schema:
# {
#   "<subject_key>": {
#     "locked_at": "2026-03-14T15:00:00Z",
#     "description": str,
#     "draft_file": str,
#     "trigger_firms": ["Gibson Dunn", "Latham & Watkins", ...]
#   },
#   ...
# }

def load_drafted_topics() -> dict:
    """Load drafted_topics.json; return {} if missing."""
    return _load_json(DRAFTED_TOPICS_FILE, {})


def is_topic_locked(subject_key: str) -> bool:
    """Return True if this subject has already been drafted."""
    drafted = load_drafted_topics()
    return subject_key in drafted


def lock_topic(
    subject_key: str,
    description: str,
    draft_file: str,
    trigger_firms: list[str],
) -> None:
    """
    Write a lock entry for subject_key in drafted_topics.json.
    Also updates alert_tracker.json to reflect locked status.
    """
    drafted = load_drafted_topics()
    drafted[subject_key] = {
        "locked_at": _now_iso(),
        "description": description,
        "draft_file": draft_file,
        "trigger_firms": trigger_firms,
    }
    _atomic_write(DRAFTED_TOPICS_FILE, drafted)

    # Mirror the lock into alert_tracker.json
    tracker_lock_subject(subject_key, draft_file)


def get_all_locked_topics() -> dict:
    """Return the full drafted_topics.json dict."""
    return load_drafted_topics()


def get_lock_info(subject_key: str) -> Optional[dict]:
    """Return the lock entry for subject_key, or None if not locked."""
    drafted = load_drafted_topics()
    return drafted.get(subject_key)


# ── Rebuild helper ─────────────────────────────────────────────────────────────

def rebuild_tracker_from_db() -> None:
    """
    Regenerate alert_tracker.json from the SQLite database.
    Useful after manual DB edits or to recover from a corrupted tracker.
    """
    # Import here to avoid circular imports at module load
    from src import database as db

    tracker: dict = {"last_updated": _now_iso(), "clusters": {}}
    clusters_db = db.get_all_clusters()

    for c in clusters_db:
        articles_db = db.get_cluster_articles(c["id"])
        articles_list = [
            {
                "firm_name": a["firm_name"],
                "title": a["title"],
                "url": a["url"],
                "date_detected": a["date_detected"],
                "date_published": a["date_published"],
            }
            for a in articles_db
        ]
        tracker["clusters"][c["subject_key"]] = {
            "subject_key": c["subject_key"],
            "description": c["description"],
            "status": c["status"],
            "firm_count": c["firm_count"],
            "earliest_pub_date": c["earliest_pub_date"],
            "latest_pub_date": c["latest_pub_date"],
            "locked_at": c["locked_at"],
            "draft_file": c["draft_file"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "articles": articles_list,
        }

    save_tracker(tracker)
