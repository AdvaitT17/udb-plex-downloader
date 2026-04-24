#!/usr/bin/env bash
# install.sh — bootstrap the UDB trigger service on Umbrel from scratch.
#
# Handles the "Umbrel OS update wiped everything" case by:
#   1. Cloning the repo (or pulling latest if already present)
#   2. Restoring SQLite queue + auth token from the latest backup found in
#      the Plex library dir (which survives Umbrel updates)
#   3. Writing a .env with UDB_TRIGGER_TOKEN and building the container
#
# Usage (run as umbrel user, from anywhere):
#   curl -fsSL https://raw.githubusercontent.com/<you>/udb/main/scripts/install.sh | bash
#
# Or if you've cloned the repo already:
#   bash ~/udb/scripts/install.sh
#
# Environment overrides:
#   UDB_REPO      — git URL to clone (default points at the upstream fork)
#   UDB_DIR       — install location (default: $HOME/udb)
#   UDB_BRANCH    — branch to check out (default: main)
#   BACKUP_DIR    — where to look for backups (default: Plex-survival path)
#   NEW_TOKEN     — set to a specific value to force a fresh token
set -euo pipefail

UDB_REPO="${UDB_REPO:-https://github.com/mahde10/udb.git}"
UDB_DIR="${UDB_DIR:-$HOME/udb}"
UDB_BRANCH="${UDB_BRANCH:-main}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/umbrel/home/Downloads/udb-backups}"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found; is this an Umbrel host?"
command -v git    >/dev/null || die "git not found"

# --- 1. fetch / update code -------------------------------------------------
if [[ -d "$UDB_DIR/.git" ]]; then
  log "repo already present at $UDB_DIR — pulling latest"
  git -C "$UDB_DIR" fetch --tags origin
  git -C "$UDB_DIR" checkout "$UDB_BRANCH"
  git -C "$UDB_DIR" pull --ff-only origin "$UDB_BRANCH"
else
  log "cloning $UDB_REPO into $UDB_DIR"
  git clone --branch "$UDB_BRANCH" "$UDB_REPO" "$UDB_DIR"
fi

cd "$UDB_DIR"

# --- 2. restore state from the latest backup, if any ------------------------
mkdir -p "$BACKUP_DIR"
latest_backup="$(ls -1t "$BACKUP_DIR"/udb-backup-*.tar.gz 2>/dev/null | head -n1 || true)"

mkdir -p "$UDB_DIR/trigger/data"

restored_token=""
if [[ -n "$latest_backup" ]]; then
  log "found backup: $latest_backup — restoring queue + token"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  tar -xzf "$latest_backup" -C "$tmp"
  # The tar contains `trigger/data/queue.sqlite` and `trigger/.env`.
  if [[ -f "$tmp/trigger/data/queue.sqlite" ]]; then
    cp "$tmp/trigger/data/queue.sqlite" "$UDB_DIR/trigger/data/queue.sqlite"
    log "  restored queue.sqlite"
  fi
  if [[ -f "$tmp/trigger/.env" ]]; then
    restored_token="$(grep -E '^UDB_TRIGGER_TOKEN=' "$tmp/trigger/.env" | cut -d= -f2- || true)"
  fi
else
  log "no backup found at $BACKUP_DIR (first install?)"
fi

# --- 3. write .env ----------------------------------------------------------
ENV_FILE="$UDB_DIR/.env"
if [[ -n "${NEW_TOKEN:-}" ]]; then
  TOKEN="$NEW_TOKEN"
  log "using token from NEW_TOKEN env var"
elif [[ -n "$restored_token" ]]; then
  TOKEN="$restored_token"
  log "restored token from backup (extension keeps working)"
elif [[ -f "$ENV_FILE" ]] && grep -q '^UDB_TRIGGER_TOKEN=' "$ENV_FILE"; then
  TOKEN="$(grep '^UDB_TRIGGER_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
  log "token already present in .env — keeping it"
else
  TOKEN="$(openssl rand -hex 16)"
  log "generated NEW token — update the Chrome extension's background.js:"
  log "    $TOKEN"
fi

cat > "$ENV_FILE" <<EOF
UDB_TRIGGER_TOKEN=$TOKEN
EOF
chmod 600 "$ENV_FILE"

# --- 4. build + start -------------------------------------------------------
log "building and starting the container"
sudo docker compose --env-file "$ENV_FILE" -f "$UDB_DIR/docker-compose.yml" \
    up -d --build

log "waiting for health check…"
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8787/health >/dev/null 2>&1; then
    log "service up: http://$(hostname):8787/dashboard"
    log "auth token: $TOKEN"
    exit 0
  fi
  sleep 1
done
die "service didn't answer /health within 30s — check: sudo docker compose logs udb-trigger"
