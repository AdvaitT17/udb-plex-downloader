#!/usr/bin/env bash
# backup.sh — snapshot UDB trigger state (queue DB + token + recent logs) to
# the Plex library dir. That dir survives Umbrel OS updates, so install.sh
# can restore from it on a wiped box.
#
# Usage:
#   bash ~/udb/scripts/backup.sh            # create one snapshot now
#   bash ~/udb/scripts/backup.sh --cron     # install a daily cron job
#
# Keeps the 14 most recent snapshots.
set -euo pipefail

UDB_DIR="${UDB_DIR:-$HOME/udb}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/umbrel/home/Downloads/udb-backups}"
KEEP="${KEEP:-14}"

log() { printf '[backup] %s\n' "$*"; }

if [[ "${1:-}" == "--cron" ]]; then
  # Umbrel OS doesn't ship `cron`; use a systemd user timer instead.
  # Runs daily at 03:30 local; logs to journalctl --user -u udb-backup.
  svc_dir="$HOME/.config/systemd/user"
  mkdir -p "$svc_dir"

  cat > "$svc_dir/udb-backup.service" <<EOF
[Unit]
Description=Snapshot UDB trigger state to Plex library
After=network-online.target

[Service]
Type=oneshot
Environment=UDB_DIR=$UDB_DIR
Environment=BACKUP_DIR=$BACKUP_DIR
Environment=KEEP=$KEEP
ExecStart=/usr/bin/env bash $UDB_DIR/scripts/backup.sh
StandardOutput=journal
StandardError=journal
EOF

  cat > "$svc_dir/udb-backup.timer" <<'EOF'
[Unit]
Description=Daily UDB trigger backup

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
Unit=udb-backup.service

[Install]
WantedBy=timers.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now udb-backup.timer
  # Allow the timer to keep running after logout (Umbrel keeps the user
  # session short-lived). Requires the box to have user lingering enabled.
  if command -v loginctl >/dev/null 2>&1; then
    sudo loginctl enable-linger "$USER" 2>/dev/null || \
      log "warning: could not enable-linger; timer only runs while you're logged in"
  fi

  log "installed systemd user timer — inspect with: systemctl --user list-timers"
  exit 0
fi

mkdir -p "$BACKUP_DIR"

ts="$(date +%Y%m%d_%H%M%S)"
out="$BACKUP_DIR/udb-backup-$ts.tar.gz"

# Collect files relative to $UDB_DIR so the tar mirrors the repo layout,
# making install.sh's extraction logic trivial.
pushd "$UDB_DIR" >/dev/null

files_to_archive=()
[[ -f ".env" ]]                        && files_to_archive+=(".env")
[[ -f "trigger/data/queue.sqlite" ]]   && files_to_archive+=("trigger/data/queue.sqlite")
# Keep recent log files too so you can look at historical runs after a wipe.
# Limit to last 50 logs to keep tar small.
if [[ -d "trigger/data/logs" ]]; then
  mapfile -t recent_logs < <(ls -1t trigger/data/logs/*.log 2>/dev/null | head -n 50)
  files_to_archive+=("${recent_logs[@]}")
fi

# install.sh expects `trigger/.env` specifically; copy .env there if present.
if [[ -f ".env" ]]; then
  mkdir -p trigger
  cp .env trigger/.env
  files_to_archive+=("trigger/.env")
fi

if [[ ${#files_to_archive[@]} -eq 0 ]]; then
  log "nothing to back up yet"
  exit 0
fi

tar -czf "$out" "${files_to_archive[@]}"
# Clean up the convenience copy
rm -f trigger/.env
popd >/dev/null

log "wrote $out ($(du -h "$out" | cut -f1))"

# Prune old backups
cd "$BACKUP_DIR"
count="$(ls -1t udb-backup-*.tar.gz 2>/dev/null | wc -l)"
if (( count > KEEP )); then
  ls -1t udb-backup-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm -v
fi
