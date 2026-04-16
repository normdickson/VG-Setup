# Dockerfile — Latitude Job Setup
#
# Simplified: no ODBC driver needed — SQL access is via Azure Function API.

FROM python:3.11-slim

WORKDIR /app

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────────────
COPY api.py models.py latitude.py postgres.py sharepoint_helper.py sited.py index.html ./

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN useradd --create-home --no-log-init appuser
USER appuser

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ── Runtime environment ───────────────────────────────────────────────────────
ENV PORT=8000 \
    LOG_LEVEL=INFO

EXPOSE ${PORT}

CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
