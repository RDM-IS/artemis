#!/usr/bin/env bash
# Artemis startup script — waits for Docker + Mattermost, then launches.
# Designed to be called from Windows Task Scheduler via WSL.
set -euo pipefail

ARTEMIS_DIR="/mnt/d/Artemis"
VENV_PYTHON="${ARTEMIS_DIR}/venv/bin/python"
LOG_FILE="${ARTEMIS_DIR}/artemis.log"
MATTERMOST_URL="http://localhost:8065"
DOCKER_MAX_WAIT=120   # seconds
MM_MAX_WAIT=120       # seconds

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [start_artemis] $*" | tee -a "$LOG_FILE"; }

# -------------------------------------------------------------------
# 1. Wait for Docker daemon
# -------------------------------------------------------------------
log "Waiting for Docker daemon..."
elapsed=0
while ! docker info >/dev/null 2>&1; do
    if [ "$elapsed" -ge "$DOCKER_MAX_WAIT" ]; then
        log "ERROR: Docker not ready after ${DOCKER_MAX_WAIT}s — aborting"
        exit 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
done
log "Docker is ready (${elapsed}s)"

# -------------------------------------------------------------------
# 2. Ensure containers are running
# -------------------------------------------------------------------
cd "$ARTEMIS_DIR"
if ! docker compose ps --status running 2>/dev/null | grep -q mattermost; then
    log "Starting Docker containers..."
    docker compose up -d 2>&1 | tee -a "$LOG_FILE"
else
    log "Docker containers already running"
fi

# -------------------------------------------------------------------
# 3. Wait for Mattermost to be healthy
# -------------------------------------------------------------------
log "Waiting for Mattermost API..."
elapsed=0
while true; do
    if curl -sf "${MATTERMOST_URL}/api/v4/system/ping" >/dev/null 2>&1; then
        break
    fi
    if [ "$elapsed" -ge "$MM_MAX_WAIT" ]; then
        log "ERROR: Mattermost not healthy after ${MM_MAX_WAIT}s — aborting"
        exit 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
done
log "Mattermost is healthy (${elapsed}s)"

# -------------------------------------------------------------------
# 4. Launch Artemis
# -------------------------------------------------------------------
log "Starting Artemis..."
cd "$ARTEMIS_DIR"
exec "$VENV_PYTHON" -m artemis.main 2>&1 | tee -a "$LOG_FILE"
