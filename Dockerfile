# ── Base image ─────────────────────────────────────────────────────────────────
# Official Playwright Python image — ships with Chromium and every system
# dependency it needs already installed.  This avoids the Ubuntu-specific
# font package (ttf-unifont, ttf-ubuntu-font-family) errors that occur when
# running `playwright install-deps` on a plain Debian/slim base image.
# The image tag is pinned to the same Playwright version as requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
# Playwright is already installed in the base image; pip will confirm the
# version matches and install the rest of the requirements.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────────────────────
COPY src/ ./src/
COPY law_firm_alerts.json .
COPY STYLE_GUIDE.md .
COPY PARAMETERS.md .

# ── Runtime directories ────────────────────────────────────────────────────────
# Railway volume mounts will overlay data/, drafts/, and logs/ at runtime,
# providing persistence across deploys and restarts.
RUN mkdir -p data drafts logs

# ── Smoke-test the import chain ────────────────────────────────────────────────
RUN python -c "from src.config import SCHEDULE_TIMES_MT, SATURATION_FIRM_COUNT; \
               print(f'Config OK — trigger={SATURATION_FIRM_COUNT} firms, schedule={SCHEDULE_TIMES_MT}')"

# ── Entry point ────────────────────────────────────────────────────────────────
CMD ["python", "-m", "src.scheduler"]
