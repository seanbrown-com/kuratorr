#!/bin/bash
set -euo pipefail

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

trap 'status=$?; log "Update failed at line $LINENO (exit $status)." >&2' ERR

if [[ $EUID -ne 0 ]]; then log "Run as root." >&2; exit 1; fi
APP_DIR=/opt/kuratorr
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="/var/backups/kuratorr/kuratorr-$STAMP.dump"
BACKUP_TIMEOUT=${KURATORR_BACKUP_TIMEOUT:-30m}
BACKUP_COMPRESSION=${KURATORR_BACKUP_COMPRESSION:-1}
GIT_TIMEOUT=${KURATORR_GIT_TIMEOUT:-5m}
PACKAGE_TIMEOUT=${KURATORR_PACKAGE_TIMEOUT:-20m}
DJANGO_TIMEOUT=${KURATORR_DJANGO_TIMEOUT:-10m}

exec 9>/run/lock/kuratorr-update.lock
if ! flock -n 9; then
  log "Another Kuratorr update is already running." >&2
  exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  log "$APP_DIR is not a Git checkout." >&2
  exit 1
fi

mkdir -p /var/backups/kuratorr

log "Backing up PostgreSQL to $BACKUP_FILE (timeout: $BACKUP_TIMEOUT; compression: $BACKUP_COMPRESSION)..."
timeout --foreground "$BACKUP_TIMEOUT" \
  runuser -u postgres -- pg_dump --format=custom --compress="$BACKUP_COMPRESSION" --verbose kuratorr > "$BACKUP_FILE"
log "Database backup complete: $(du -h "$BACKUP_FILE" | cut -f1)."

log "Pulling the latest fast-forward Git revision (timeout: $GIT_TIMEOUT)..."
timeout --foreground "$GIT_TIMEOUT" \
  runuser -u kuratorr -- env GIT_TERMINAL_PROMPT=0 \
  git -C "$APP_DIR" pull --ff-only

log "Installing Python dependencies (timeout: $PACKAGE_TIMEOUT)..."
timeout --foreground "$PACKAGE_TIMEOUT" \
  env PIP_DEFAULT_TIMEOUT=60 \
  "$APP_DIR/.venv/bin/pip" install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

log "Applying database migrations (timeout: $DJANGO_TIMEOUT)..."
timeout --foreground "$DJANGO_TIMEOUT" \
  runuser -u kuratorr -- "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" migrate --noinput

log "Collecting static assets (timeout: $DJANGO_TIMEOUT)..."
timeout --foreground "$DJANGO_TIMEOUT" \
  runuser -u kuratorr -- "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" collectstatic --noinput

log "Restarting Kuratorr services..."
systemctl restart kuratorr-web kuratorr-worker kuratorr-beat
systemctl reload nginx

log "Service status:"
systemctl --no-pager --full status kuratorr-web kuratorr-worker kuratorr-beat
log "Kuratorr update completed successfully."
