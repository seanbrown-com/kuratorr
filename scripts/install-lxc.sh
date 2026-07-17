#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run as root inside a Debian 13 LXC." >&2; exit 1; fi
REPO_URL=${1:?Usage: install-lxc.sh REPO_URL DOMAIN [LETSENCRYPT_EMAIL]}
DOMAIN=${2:?Usage: install-lxc.sh REPO_URL DOMAIN [LETSENCRYPT_EMAIL]}
EMAIL=${3:-}
APP_DIR=/opt/music-library-curator
APP_USER=musiclibrary

apt-get update
apt-get install -y git python3 python3-venv python3-dev build-essential libpq-dev postgresql redis-server nginx certbot python3-certbot-nginx openssl
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
if [[ -d "$APP_DIR/.git" ]]; then echo "$APP_DIR already exists; use scripts/update-from-git.sh" >&2; exit 1; fi
git clone "$REPO_URL" "$APP_DIR"

DB_PASSWORD=$(openssl rand -hex 24)
DJANGO_SECRET=$(openssl rand -hex 48)
SETUP_TOKEN=$(openssl rand -hex 24)
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='music_library'" | grep -q 1; then
  runuser -u postgres -- psql -c "CREATE USER music_library WITH PASSWORD '$DB_PASSWORD';"
else
  runuser -u postgres -- psql -c "ALTER USER music_library WITH PASSWORD '$DB_PASSWORD';"
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='music_library'" | grep -q 1; then
  runuser -u postgres -- createdb -O music_library music_library
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
cat > "$APP_DIR/.env" <<EOF
DJANGO_SECRET_KEY=$DJANGO_SECRET
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=$DOMAIN
DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN
DJANGO_SECURE_COOKIES=true
DJANGO_SSL_REDIRECT=true
DJANGO_HSTS_SECONDS=31536000
TIME_ZONE=UTC
INITIAL_SETUP_TOKEN=$SETUP_TOKEN
DATABASE_URL=postgresql://music_library:$DB_PASSWORD@localhost:5432/music_library
CELERY_BROKER_URL=redis://localhost:6379/0
HTTP_USER_AGENT=MusicLibraryCurator/1.0 ($DOMAIN)
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
LASTFM_API_KEY=
YOUTUBE_API_KEY=
EOF
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" migrate --noinput
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" collectstatic --noinput

install -m 0644 "$APP_DIR/deploy/systemd/music-library-web.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/systemd/music-library-worker.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/systemd/music-library-beat.service" /etc/systemd/system/
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/deploy/nginx/music-library.conf" > /etc/nginx/sites-available/music-library
ln -sf /etc/nginx/sites-available/music-library /etc/nginx/sites-enabled/music-library
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now postgresql redis-server music-library-web music-library-worker music-library-beat nginx
systemctl reload nginx
if [[ -n "$EMAIL" ]]; then certbot --nginx --non-interactive --agree-tos -m "$EMAIL" -d "$DOMAIN" --redirect; fi
echo "Installation complete: https://$DOMAIN"
echo "ONE-TIME INITIAL SETUP TOKEN: $SETUP_TOKEN"
echo "Add API credentials to $APP_DIR/.env, then restart the three music-library services."
