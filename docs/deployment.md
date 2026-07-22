# Debian 13 deployment runbook

## Before installation

1. Create an unprivileged Debian 13 LXC with adequate CPU, memory, and database storage.
2. Mount the music library read-only where possible; mount a separate writable playlist destination if desired.
3. Point a DNS record at the public address and forward TCP 80/443.
4. Configure the LXC firewall and Proxmox firewall. SSH should be key-only and restricted separately.
5. Obtain Spotify, Last.fm, and YouTube API credentials.

## Install

Run `scripts/install-lxc.sh DOMAIN_OR_IP [LETSENCRYPT_EMAIL]` as root. The installer clones the official repository from `https://github.com/seanbrown-com/kuratorr.git`; set `KURATORR_REPO_URL` only to install a fork or use an authenticated clone URL.

The first argument tells Django and Nginx which hostname or IP address will receive requests. With no email, the installer creates a LAN-friendly HTTP deployment and does not enable secure cookies, HTTPS redirects, or HSTS. For an internet-facing deployment, point a DNS hostname at the service and provide the optional email. Certbot then requests a Let's Encrypt certificate, updates Nginx for HTTPS, and the installer enables the corresponding Django security settings.

Examples:

```bash
# Internet-facing HTTPS installation
./scripts/install-lxc.sh kuratorr.example.com admin@example.com

# LAN-only HTTP installation by IP address
./scripts/install-lxc.sh 192.168.1.50
```

The application lives at `/opt/kuratorr`; infrastructure secrets live in its mode-0600 `.env`; systemd runs all application processes as `kuratorr`. The installer generates `en_US.UTF-8` and explicitly creates the PostgreSQL database with UTF-8 encoding so enrichment data can safely contain international text and punctuation. Provider credentials may be entered in the Settings UI and are encrypted in PostgreSQL using a key derived from `DJANGO_SECRET_KEY`. Back up the database and `.env` together; neither is sufficient to recover those credentials alone. Never commit `.env`.

## Mount permissions

The `kuratorr` account needs traverse/read access to every library parent and file. It needs create/write/rename access to playlist output directories. If the source mount is under `/home`, the systemd `ProtectHome=true` policy blocks it; use `/mnt`, `/media`, or another system mount point, or deliberately adjust the unit after understanding the exposure.

After editing units or `.env`:

```bash
systemctl daemon-reload
systemctl restart kuratorr-web kuratorr-worker kuratorr-beat
```

## Observe

```bash
systemctl status kuratorr-web kuratorr-worker kuratorr-beat
journalctl -u kuratorr-worker -f
nginx -t
curl -fsS https://YOUR_DOMAIN/health/
```

## Update and recover

Run `scripts/update-from-git.sh` as root. The updater prints and times every stage, shows verbose PostgreSQL backup progress, refuses concurrent update runs, and disables interactive Git credential prompts. Backups are written under `/var/backups/kuratorr` before code or schema changes. Retention is intentionally left to the host's backup policy. Custom-format backups use fast level-1 compression by default. Override it with `KURATORR_BACKUP_COMPRESSION` (`0` favors maximum speed and disk usage); the stage timeouts can be overridden with `KURATORR_BACKUP_TIMEOUT`, `KURATORR_GIT_TIMEOUT`, `KURATORR_PACKAGE_TIMEOUT`, and `KURATORR_DJANGO_TIMEOUT`.

To restore a backup, stop application services, create/empty the target database according to your recovery policy, then use `pg_restore`. Test restoration on a non-production database before relying on it.

## Reset the catalog

To deliberately discard the entire catalog and start over, first update the
checkout so the new schema and reset script are present, then run:

```bash
sudo /opt/kuratorr/scripts/reset-database.sh --yes-delete-everything
```

This recreates PostgreSQL with UTF-8 encoding and applies every migration. It
permanently deletes users, application settings, library roots, tracks,
enrichment data, jobs, and playlists. `/opt/kuratorr/.env` is not changed, so
environment-based API credentials and service configuration are retained.
Queued Celery work is purged from Redis before the database is recreated, so
tasks cannot resume with identifiers from the deleted catalog.
Return to Kuratorr afterward to create the sole admin account again, restore the
Settings and library root, and run **Configure and Scan**. Enrichment remains a
separate manual step.

Library scans have a separate 24-hour hard limit and 23-hour soft limit by
default. Override these in `.env` with `SCAN_TASK_TIME_LIMIT` and
`SCAN_TASK_SOFT_TIME_LIMIT`, expressed in seconds. The soft limit must be lower
than the hard limit. Other Celery jobs retain the shorter global limits.

## Security checklist

- HTTPS works and HTTP redirects to HTTPS.
- `DJANGO_DEBUG=false`, secure cookies enabled, HSTS enabled only after HTTPS works.
- HSTS preload is intentionally not enabled automatically; opt in only after understanding the long-lived, subdomain-wide preload commitment.
- Only the expected domain is in allowed hosts and CSRF trusted origins.
- The setup token is long, secret, and used only once; rotate/remove it from `.env` after the administrator is created.
- Provider keys are encrypted in PostgreSQL or supplied through `.env`; database and Django secrets remain in `.env` with mode 0600.
- Library mount is read-only unless another process requires writes.
- PostgreSQL and Redis listen only on localhost/container-private networks.
- OS security updates, Proxmox backups, PostgreSQL backups, and log rotation are configured.
- Job errors and pending review counts are monitored from the dashboard.
