#!/bin/bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: reset-database.sh --yes-delete-everything

Permanently deletes and recreates the Kuratorr PostgreSQL database as UTF-8,
then applies all migrations. The .env file and its API credentials are retained,
but all users, settings, library roots, scans, enrichment data, jobs, and playlists
in the database are deleted.
EOF
}

if [[ ${1:-} != "--yes-delete-everything" || $# -ne 1 ]]; then
  usage
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Run as root on the Kuratorr LXC." >&2
  exit 1
fi

APP_DIR=/opt/kuratorr
APP_USER=kuratorr
DB_NAME=kuratorr
DB_USER=kuratorr
DB_LOCALE=en_US.UTF-8

if ! locale -a | grep -Eiq '^en_US\.utf-?8$'; then
  DB_LOCALE=C.UTF-8
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "Kuratorr virtual environment not found at $APP_DIR/.venv." >&2
  exit 1
fi

echo "Stopping Kuratorr services..."
systemctl stop kuratorr-web kuratorr-worker kuratorr-beat

restart_services() {
  systemctl start kuratorr-web kuratorr-worker kuratorr-beat
}
trap restart_services EXIT

echo "Purging queued Celery tasks from Redis..."
runuser -u "$APP_USER" -- env PYTHONPATH="$APP_DIR" \
  "$APP_DIR/.venv/bin/celery" -A config purge --force

echo "Terminating open database sessions..."
runuser -u postgres -- psql --dbname=postgres --set=ON_ERROR_STOP=1 --command \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();"

echo "Recreating the database as UTF-8..."
runuser -u postgres -- dropdb --if-exists "$DB_NAME"
runuser -u postgres -- createdb \
  --owner="$DB_USER" \
  --encoding=UTF8 \
  --locale="$DB_LOCALE" \
  --template=template0 \
  "$DB_NAME"

echo "Applying migrations..."
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" migrate --noinput

echo "Database reset complete. Kuratorr services are restarting."
