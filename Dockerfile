# Backend agent server, for Cloud Run.
#
# SCOPE: this image runs ONLY the Gemini-facing agent pipeline (Orchestrator
# -> Planner -> Action) and its health check. Jarvis's actual device control
# - Spotify, Reminders, browser automation - needs macOS Accessibility APIs
# and a real screen, and cannot run here. Those modules still get copied in
# and import cleanly (the pyobjc frameworks are absent on Linux, so the
# imports are guarded and the Mac-only functions raise a clear error if
# called), but nothing in this deployment ever calls them. See planning.md
# for the "deployed to production infra" vs "the full product runs in the
# cloud" distinction.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so this layer caches across code-only changes.
COPY backend/requirements-cloudrun.txt .
RUN pip install -r requirements-cloudrun.txt

COPY backend/ ./backend/
WORKDIR /app/backend

# Cloud Run injects PORT (usually 8080) and K_SERVICE. agent_server.py reads
# both: binds 0.0.0.0:$PORT, serves GET /health, and skips the browser
# bridge (nothing to bridge to in the cloud). No secrets are baked in -
# GOOGLE_API_KEY is supplied as a Cloud Run env var at deploy time.
CMD ["python", "servers/agent_server.py"]
