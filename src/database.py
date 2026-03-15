"""
src/database.py
SQLite database schema and all data-access operations.

Tables
------
articles        — every scraped article from every competitor firm
clusters        — subject clusters that may trigger drafting
cluster_articles — junction linking articles → clusters (many-to-many)
scrape_log      — one row per firm per scan (success or failure)
"""

import sqlite3
import contextlib
from datetime import datetime, timezone
from typing import Optional

from src.config import DB_FILE

# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_name       TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    url             TEXT    NOT NULL UNIQUE,
    date_published  TEXT,               -- ISO date string or NULL if unknown
    date_detected   TEXT    NOT NULL,   -- ISO datetime of first detection
    is_in_scope     INTEGER NOT NULL DEFAULT 0,   -- 1 = relevant practice area
    full_text       TEXT,               -- extracted body text, may be NULL
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clusters (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_key       TEXT    NOT NULL UNIQUE,
    description       TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'active', -- 'active' | 'locked'
    firm_count        INTEGER NOT NULL DEFAULT 0,
    earliest_pub_date TEXT,
    latest_pub_date   TEXT,
    locked_at         TEXT,   -- ISO datetime when draft was triggered
    draft_file        TEXT,   -- relative path to generated .docx
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cluster_articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id  INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    UNIQUE(cluster_id, article_id)
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    firm_name       TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    scan_time       TEXT    NOT NULL DEFAULT (datetime('now')),
    success         INTEGER NOT NULL DEFAULT 1,   -- 1 = OK, 0 = error
    error_message   TEXT,
    articles_found  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_articles_firm    ON articles(firm_name);
CREATE INDEX IF NOT EXISTS idx_articles_date    ON articles(date_detected);
CREATE INDEX IF NOT EXISTS idx_clusters_status  ON clusters(status);
CREATE INDEX IF NOT EXISTS idx_scrape_firm_time ON scrape_log(firm_name, scan_time);
"""


# ── Connection helper ──────────────────────────────────────────────────────────

@contextlib.contextmanager
def get_conn():
    """Yield a sqlite3.Connection, commit on exit, rollback on error."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables/indexes if they don't exist, then run migrations."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Idempotent column-addition migrations.
    Each ALTER TABLE is wrapped in try/except so repeated calls are safe.
    """
    # Phase 4: track when an article was processed by the classifier
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN classified_at TEXT")
        conn.commit()
    except Exception:
        pass  # column already present


# ── Utility ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Articles ───────────────────────────────────────────────────────────────────

def upsert_article(
    firm_name: str,
    title: str,
    url: str,
    date_published: Optional[str],
    is_in_scope: bool,
    full_text: Optional[str] = None,
    date_detected: Optional[str] = None,
) -> Optional[int]:
    """
    Insert a new article or ignore if the URL already exists.
    Returns the row id (new or existing), or None on error.
    """
    detected = date_detected or _now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO articles
                (firm_name, title, url, date_published, date_detected,
                 is_in_scope, full_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO NOTHING
            """,
            (firm_name, title, url, date_published, detected,
             int(is_in_scope), full_text),
        )
        if cur.lastrowid:
            return cur.lastrowid
        # Row already existed — fetch its id
        row = conn.execute(
            "SELECT id FROM articles WHERE url = ?", (url,)
        ).fetchone()
        return row["id"] if row else None


def get_article_by_url(url: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM articles WHERE url = ?", (url,)
        ).fetchone()


def article_exists(url: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE url = ?", (url,)
        ).fetchone()
        return row is not None


def get_unclassified_articles(limit: int = 500) -> list[sqlite3.Row]:
    """Return articles that have not yet been processed by the classifier."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM articles
            WHERE classified_at IS NULL
            ORDER BY date_detected ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def mark_article_classified(
    article_id: int,
    is_in_scope: bool,
) -> None:
    """Record that the classifier has processed this article."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE articles
            SET classified_at = ?,
                is_in_scope   = ?
            WHERE id = ?
            """,
            (_now_iso(), int(is_in_scope), article_id),
        )


def get_article_by_id(article_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()


def get_in_scope_articles_since(cutoff_iso: str) -> list[sqlite3.Row]:
    """Return all in-scope articles with date_detected >= cutoff."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM articles
            WHERE is_in_scope = 1 AND date_detected >= ?
            ORDER BY date_detected DESC
            """,
            (cutoff_iso,),
        ).fetchall()


# ── Clusters ───────────────────────────────────────────────────────────────────

def upsert_cluster(
    subject_key: str,
    description: str,
) -> int:
    """Insert cluster if new, return its id either way."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO clusters (subject_key, description)
            VALUES (?, ?)
            ON CONFLICT(subject_key) DO NOTHING
            """,
            (subject_key, description),
        )
        row = conn.execute(
            "SELECT id FROM clusters WHERE subject_key = ?", (subject_key,)
        ).fetchone()
        return row["id"]


def link_article_to_cluster(cluster_id: int, article_id: int) -> None:
    """Associate an article with a cluster (idempotent)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cluster_articles (cluster_id, article_id)
            VALUES (?, ?)
            ON CONFLICT DO NOTHING
            """,
            (cluster_id, article_id),
        )


def refresh_cluster_stats(cluster_id: int) -> None:
    """
    Recompute firm_count, earliest_pub_date, latest_pub_date from the
    articles linked to this cluster and persist them.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT a.firm_name)        AS firm_count,
                MIN(a.date_published)              AS earliest,
                MAX(a.date_published)              AS latest
            FROM cluster_articles ca
            JOIN articles a ON a.id = ca.article_id
            WHERE ca.cluster_id = ?
              AND a.date_published IS NOT NULL
            """,
            (cluster_id,),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE clusters
                SET firm_count        = ?,
                    earliest_pub_date = ?,
                    latest_pub_date   = ?,
                    updated_at        = ?
                WHERE id = ?
                """,
                (row["firm_count"], row["earliest"], row["latest"],
                 _now_iso(), cluster_id),
            )


def get_cluster_by_key(subject_key: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clusters WHERE subject_key = ?", (subject_key,)
        ).fetchone()


def get_active_clusters() -> list[sqlite3.Row]:
    """Return all clusters with status = 'active'."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clusters WHERE status = 'active'"
        ).fetchall()


def get_cluster_articles(cluster_id: int) -> list[sqlite3.Row]:
    """Return all articles linked to a cluster."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT a.*
            FROM articles a
            JOIN cluster_articles ca ON ca.article_id = a.id
            WHERE ca.cluster_id = ?
            ORDER BY a.date_published ASC
            """,
            (cluster_id,),
        ).fetchall()


def get_cluster_firm_names(cluster_id: int) -> list[str]:
    """Return the list of distinct firm names contributing to a cluster."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT a.firm_name
            FROM articles a
            JOIN cluster_articles ca ON ca.article_id = a.id
            WHERE ca.cluster_id = ?
            """,
            (cluster_id,),
        ).fetchall()
        return [r["firm_name"] for r in rows]


def lock_cluster(cluster_id: int, draft_file: str) -> None:
    """Mark a cluster as locked (draft generated). Idempotent."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE clusters
            SET status     = 'locked',
                locked_at  = ?,
                draft_file = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (_now_iso(), draft_file, _now_iso(), cluster_id),
        )


def get_all_clusters() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM clusters ORDER BY created_at DESC"
        ).fetchall()


# ── Scrape log ─────────────────────────────────────────────────────────────────

def log_scrape(
    firm_name: str,
    url: str,
    success: bool,
    articles_found: int = 0,
    error_message: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scrape_log
                (firm_name, url, success, articles_found, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (firm_name, url, int(success), articles_found, error_message),
        )


def get_last_scrape(firm_name: str) -> Optional[sqlite3.Row]:
    """Return the most recent scrape log entry for a firm."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM scrape_log
            WHERE firm_name = ?
            ORDER BY scan_time DESC
            LIMIT 1
            """,
            (firm_name,),
        ).fetchone()


def get_scrape_errors_since(cutoff_iso: str) -> list[sqlite3.Row]:
    """Return all failed scrape attempts since cutoff."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM scrape_log
            WHERE success = 0 AND scan_time >= ?
            ORDER BY scan_time DESC
            """,
            (cutoff_iso,),
        ).fetchall()
