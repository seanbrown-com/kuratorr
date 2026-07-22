#!/bin/bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: install-lxc.sh DOMAIN_OR_IP [LETSENCRYPT_EMAIL]

Examples:
  install-lxc.sh kuratorr.example.com admin@example.com  # Public HTTPS
  install-lxc.sh 192.168.1.50                            # LAN HTTP

Kuratorr is cloned from https://github.com/seanbrown-com/kuratorr.git.
Set KURATORR_REPO_URL only when installing from a fork or authenticated clone URL.
EOF
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then usage; exit 0; fi
if [[ $# -lt 1 || $# -gt 2 ]]; then usage; exit 1; fi
if [[ $EUID -ne 0 ]]; then echo "Run as root inside a Debian 13 LXC." >&2; exit 1; fi

REPO_URL=${KURATORR_REPO_URL:-https://github.com/seanbrown-com/kuratorr.git}
DOMAIN=$1
EMAIL=${2:-}
APP_DIR=/opt/kuratorr
APP_USER=kuratorr

if [[ -n "$EMAIL" ]]; then
  URL_SCHEME=https
  SECURE_COOKIES=true
  SSL_REDIRECT=true
  HSTS_SECONDS=31536000
else
  URL_SCHEME=http
  SECURE_COOKIES=false
  SSL_REDIRECT=false
  HSTS_SECONDS=0
fi

apt-get update
# Minimal LXC images may advertise en_US.UTF-8 without actually providing it.
# Generate it before PostgreSQL is installed so the cluster and application
# database are never initialized with the lossy SQL_ASCII encoding.
apt-get install -y locales
sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
locale-gen en_US.UTF-8
update-locale LANG=en_US.UTF-8
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

apt-get install -y git python3 python3-venv python3-dev build-essential libpq-dev postgresql redis-server nginx certbot python3-certbot-nginx openssl
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
if [[ -d "$APP_DIR/.git" ]]; then echo "$APP_DIR already exists; use scripts/update-from-git.sh" >&2; exit 1; fi
git clone "$REPO_URL" "$APP_DIR"

DB_PASSWORD=$(openssl rand -hex 24)
DJANGO_SECRET=$(openssl rand -hex 48)
SETUP_TOKEN=$(openssl rand -hex 24)
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='kuratorr'" | grep -q 1; then
  runuser -u postgres -- psql -c "CREATE USER kuratorr WITH PASSWORD '$DB_PASSWORD';"
else
  runuser -u postgres -- psql -c "ALTER USER kuratorr WITH PASSWORD '$DB_PASSWORD';"
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='kuratorr'" | grep -q 1; then
  runuser -u postgres -- createdb \
    --owner=kuratorr \
    --encoding=UTF8 \
    --locale=en_US.UTF-8 \
    --template=template0 \
    kuratorr
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
cat > "$APP_DIR/.env" <<EOF
DJANGO_SECRET_KEY=$DJANGO_SECRET
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=$DOMAIN
DJANGO_CSRF_TRUSTED_ORIGINS=$URL_SCHEME://$DOMAIN
DJANGO_SECURE_COOKIES=$SECURE_COOKIES
DJANGO_SSL_REDIRECT=$SSL_REDIRECT
DJANGO_HSTS_SECONDS=$HSTS_SECONDS
TIME_ZONE=UTC
INITIAL_SETUP_TOKEN=$SETUP_TOKEN
DATABASE_URL=postgresql://kuratorr:$DB_PASSWORD@localhost:5432/kuratorr
CELERY_BROKER_URL=redis://localhost:6379/0
HTTP_USER_AGENT=Kuratorr/1.0 ($DOMAIN)
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
LASTFM_API_KEY=
YOUTUBE_API_KEY=
EOF
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" migrate --noinput
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" collectstatic --noinput

install -m 0644 "$APP_DIR/deploy/systemd/kuratorr-web.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/systemd/kuratorr-worker.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/systemd/kuratorr-beat.service" /etc/systemd/system/
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/deploy/nginx/kuratorr.conf" > /etc/nginx/sites-available/kuratorr
ln -sf /etc/nginx/sites-available/kuratorr /etc/nginx/sites-enabled/kuratorr
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now postgresql redis-server kuratorr-web kuratorr-worker kuratorr-beat nginx
systemctl reload nginx
if [[ -n "$EMAIL" ]]; then certbot --nginx --non-interactive --agree-tos -m "$EMAIL" -d "$DOMAIN" --redirect; fi
echo "Installation complete: $URL_SCHEME://$DOMAIN"
echo "ONE-TIME INITIAL SETUP TOKEN: $SETUP_TOKEN"
echo "Add API credentials to $APP_DIR/.env, then restart the three Kuratorr services."
