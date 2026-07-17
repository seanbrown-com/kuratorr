#!/bin/bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run as root." >&2; exit 1; fi
APP_DIR=/opt/kuratorr
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p /var/backups/kuratorr
runuser -u postgres -- pg_dump -Fc kuratorr > "/var/backups/kuratorr/kuratorr-$STAMP.dump"
cd "$APP_DIR"
git pull --ff-only
"$APP_DIR/.venv/bin/pip" install -r requirements.txt
runuser -u kuratorr -- "$APP_DIR/.venv/bin/python" manage.py migrate --noinput
runuser -u kuratorr -- "$APP_DIR/.venv/bin/python" manage.py collectstatic --noinput
systemctl restart kuratorr-web kuratorr-worker kuratorr-beat
systemctl reload nginx
systemctl --no-pager --full status kuratorr-web kuratorr-worker kuratorr-beat
