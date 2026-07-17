#!/bin/bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Run as root." >&2; exit 1; fi
APP_DIR=/opt/music-library-curator
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p /var/backups/music-library-curator
runuser -u postgres -- pg_dump -Fc music_library > "/var/backups/music-library-curator/music-library-$STAMP.dump"
cd "$APP_DIR"
git pull --ff-only
"$APP_DIR/.venv/bin/pip" install -r requirements.txt
runuser -u musiclibrary -- "$APP_DIR/.venv/bin/python" manage.py migrate --noinput
runuser -u musiclibrary -- "$APP_DIR/.venv/bin/python" manage.py collectstatic --noinput
systemctl restart music-library-web music-library-worker music-library-beat
systemctl reload nginx
systemctl --no-pager --full status music-library-web music-library-worker music-library-beat
