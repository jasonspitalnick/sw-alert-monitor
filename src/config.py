"""
Central configuration module.
Loads all environment variables and exposes project-wide constants.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Paths ──────────────────────────────────────────────────────────────────────
# Project root is one level above this file (src/)
PROJECT_ROOT = Path(__file__).parent.parent

DATA_DIR = PROJECT_ROOT / "data"
DRAFTS_DIR = PROJECT_ROOT / "drafts"
LOGS_DIR = PROJECT_ROOT / "logs"

FIRMS_FILE = PROJECT_ROOT / "law_firm_alerts.json"
STYLE_GUIDE_FILE = PROJECT_ROOT / "STYLE_GUIDE.md"
DB_FILE = DATA_DIR / "alerts.db"
ALERT_TRACKER_FILE = DATA_DIR / "alert_tracker.json"
DRAFTED_TOPICS_FILE = DATA_DIR / "drafted_topics.json"

# Ensure runtime directories exist
for _d in (DATA_DIR, DRAFTS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── Environment ────────────────────────────────────────────────────────────────
# override=True: .env file always wins over empty/placeholder shell env vars
load_dotenv(PROJECT_ROOT / ".env", override=True)

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
SENDGRID_API_KEY: str = os.environ.get("SENDGRID_API_KEY", "")
EMAIL_FROM: str = os.environ.get("EMAIL_FROM", "jason.spitalnick@gmail.com")
EMAIL_TO: str = os.environ.get("EMAIL_TO", "jspitalnick@swlaw.com")

# ── Scraping ───────────────────────────────────────────────────────────────────
# Per-page timeout in milliseconds for Playwright
PAGE_TIMEOUT_MS: int = 30_000
# Max articles to extract per firm per scan (prevents runaway on large pages)
MAX_ARTICLES_PER_FIRM: int = 50

# ── Trigger rules ──────────────────────────────────────────────────────────────
SATURATION_FIRM_COUNT: int = 2          # Minimum distinct firms to trigger
SATURATION_WINDOW_DAYS: int = 10        # Rolling window in days

# Snell & Wilmer publications URL (gap validation only)
SW_PUBLICATIONS_URL: str = "https://www.swlaw.com/publications/?all"
SW_FIRM_NAME: str = "Snell & Wilmer"

# Firms that have merged and should be counted as ONE for trigger purposes
MERGED_FIRM_GROUPS: list[list[str]] = [
    ["Locke Lord", "Troutman Pepper"],   # Troutman Pepper Locke as of Jan 2025
]

# ── Models ─────────────────────────────────────────────────────────────────────
# Fast/cheap model for per-article classification (called ~hundreds of times/scan)
CLASSIFIER_MODEL: str = "claude-haiku-4-5"
# Most capable model for generating the ~1,200-word draft alert
DRAFT_MODEL: str = "claude-opus-4-6"
DRAFT_TARGET_WORDS: int = 1_200
DRAFT_BYLINE: str = ""

# ── Scheduling ─────────────────────────────────────────────────────────────────
# Times in Mountain Time (America/Denver); weekdays only (Mon–Fri)
SCHEDULE_TIMES_MT: list[str] = ["07:00"]
SCHEDULE_WEEKDAYS_ONLY: bool = True

# ── Validation ─────────────────────────────────────────────────────────────────
def validate_env() -> list[str]:
    """Return a list of missing required environment variable names."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not SENDGRID_API_KEY:
        missing.append("SENDGRID_API_KEY")
    return missing
