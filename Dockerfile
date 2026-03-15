# ── Base image ─────────────────────────────────────────────────────────────────
# Python 3.12 slim (compatible with all code; 3.14 not yet in cloud buildpacks)
FROM python:3.12-slim

WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
# Copy requirements first so Docker caches this layer until requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Playwright + Chromium ──────────────────────────────────────────────────────
# install-deps: installs Chromium's system libraries (libnss3, libgbm1, etc.)
# install chromium: downloads the Chromium binary bundled with this playwright
RUN playwright install-deps chromium && \
    playwright install chromium

# ── Application code ───────────────────────────────────────────────────────────
# Copy only what the running app needs (venv/, tests/, data/ are excluded)
COPY src/ ./src/
COPY law_firm_alerts.json .
COPY STYLE_GUIDE.md .
COPY PARAMETERS.md .

# ── Runtime directories ────────────────────────────────────────────────────────
# Created here so config.py's mkdir() calls are no-ops at startup.
# Railway volume mounts will overlay data/, drafts/, and logs/ at runtime,
# providing persistence across deploys and restarts.
RUN mkdir -p data drafts logs

# ── Smoke-test the import chain ────────────────────────────────────────────────
RUN python -c "from src.config import SCHEDULE_TIMES_MT, SATURATION_FIRM_COUNT; \
               print(f'Config OK — trigger={SATURATION_FIRM_COUNT} firms, schedule={SCHEDULE_TIMES_MT}')"

# ── Entry point ────────────────────────────────────────────────────────────────
CMD ["python", "-m", "src.scheduler"]
