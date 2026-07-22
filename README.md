# Kuratorr

Kuratorr is a private, single-administrator Django service that scans mounted MP3 and FLAC libraries, preserves their tags, enriches artists and albums from independent external sources, identifies noteworthy local tracks, and builds browsable and exportable M3U playlists. The original hand-written prototypes are preserved in [`legacy/`](legacy/).

## What it does

- Scans one or more server-side directories without uploading audio.
- Reads MP3 ID3 and FLAC Vorbis metadata, technical audio properties, filesystem size, and modification time.
- Stores artists, albums, tracks, album-level genres, the selected file metadata used by the application, and scan errors.
- Keeps local, MusicBrainz, Spotify, Last.fm, Wikipedia, and YouTube observations separate with source payloads, URLs, IDs, timestamps, match confidence, and review decisions.
- Uses Spotify artist-top-track ranking and Last.fm play counts independently. Spotify does not expose raw public play counts.
- Extracts Wikipedia singles and music-video mentions from inconsistent tables and lists.
- Accepts YouTube candidates automatically only when both the local track match and official-video heuristic are strong. Lyrics, official-audio, visualizer, audio-only, and fan-video candidates are excluded.
- Uses MusicBrainz release groups, genres/tags, and artist relationships; uses Last.fm similar artists.
- Retains related artists even when they are absent from the library, then ranks recommendations by the number of distinct library artists linking to them while preserving source evidence.
- Compares MusicBrainz album catalogs with the local collection and lists absent releases with source-qualified notable tracks on a filterable Missing page.
- Selects up to three genres per album, with manual assignments taking precedence.
- Builds Best of Artist, Year, Decade, Genre, Genre+Year, Genre+Decade, and Artist Radio playlists.
- Requires aggregate and radio playlists to meet the configurable minimum duration (one hour by default).
- Stores playlists and ordered entries in PostgreSQL, with optional atomic M3U materialization beneath one server output directory, organized by playlist type.
- Supports soft deletion, permanent regeneration suppression, a deleted-playlist review screen, and restoration.
- Downloads individual M3U files, a browser-delivered ZIP containing all active playlists in type folders, or a two-argument Bash script that copies a playlist into a same-named destination directory.
- Records every manually or automatically started job and its outcome.

## Quick local start (Docker)

Requirements: Docker with Compose, OpenSSL, mounted music and output directories.

```bash
./scripts/setup-local.sh
```

On first use:

1. Save the setup token printed by the script.
2. Edit `.env`: set `MUSIC_MOUNT_HOST`, `LIBRARY_BROWSE_MOUNT_HOST`,
   `PLAYLIST_MOUNT_HOST`, host settings, and API credentials. On macOS, point
   `LIBRARY_BROWSE_MOUNT_HOST` at the specific Finder-mounted share, such as
   `/Volumes/MyMusicServer`.
3. Run `./scripts/run-local.sh` to build and start the services.
4. Open `http://localhost:8000/setup/` and create the sole administrator using the token.
5. Add a library root with the server browser. The browsable parent is `/libraries`
   inside Docker (`/libraries/ShareName` for a macOS share mounted at
   `/Volumes/ShareName`). Add `/playlists` as an optional playlist output root.

### macOS network shares

Mount an SMB/NFS share in Finder first; macOS normally places it under `/Volumes`.
The Docker services mount the host directory configured by
`LIBRARY_BROWSE_MOUNT_HOST` read-only at `/libraries`, and both the web process and
background scanner see the same paths. If Docker Desktop requests permission to
access a network volume, grant it; otherwise add the relevant host path under
Docker Desktop's file-sharing settings. Mount the specific share rather than the
whole `/Volumes` directory, because probing disconnected or sleeping volumes can
make directory browsing stall.

Useful commands:

```bash
docker compose logs -f web worker beat
docker compose exec web python manage.py check
docker compose exec web pytest -q
docker compose down
```

## API credentials

Enter provider credentials on the authenticated Settings page. They are encrypted
with the service secret before database storage and password fields are never sent
back to the browser. Existing environment variables remain supported as fallbacks:

- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`
- `LASTFM_API_KEY`
- `YOUTUBE_API_KEY`
- `HTTP_USER_AGENT` (use a real contact address for Wikimedia and MusicBrainz etiquette)

MusicBrainz and Wikipedia do not require credentials. MusicBrainz requires a
meaningful, contactable HTTP User-Agent and limits clients to approximately one
request per second. Transient TLS, timeout, and server failures use bounded
backoff. A 429 opens a shared Redis-backed provider cooldown and stores an
exponentially delayed retry time for that artist/source; YouTube daily-quota
exhaustion pauses YouTube requests for 24 hours. Celery Beat requeues only eligible
work in bounded batches. A source without credentials is recorded as skipped
rather than failing the artist job; saving new credentials makes that source
eligible for automatic enrichment again.

## Processing flow

1. An administrator adds a mounted library root and starts a scan.
2. The worker reads changed/new MP3 and FLAC files, preserves raw metadata, and marks disappeared files unavailable.
3. A complete scan stops after importing local tags and filesystem metadata; enrichment is started manually from the dashboard.
4. Manual library enrichment queues one child job per available artist. Each source stores its response separately, proposes entity matches, and records noteworthy evidence while the parent job tracks child completion.
5. Whole-title matches at or above the configured acceptance threshold are accepted; only close ambiguous matches enter Review, while unrelated titles are rejected automatically.
6. Album genres are selected from accepted source evidence unless manually overridden.
7. Playlist generation uses accepted noteworthy evidence and accepted related-artist evidence.
8. Database playlists may optionally be written as M3U files to each enabled output directory.

Queued and running jobs can be cancelled from Job History. Jobs without a worker heartbeat are reconciled to Failed instead of remaining Running indefinitely.

## Native development

PostgreSQL and Redis are the supported service configuration. SQLite is provided only for tests and lightweight development when `DATABASE_URL` is absent.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py runserver
```

In separate terminals:

```bash
.venv/bin/celery -A config worker --loglevel=INFO
.venv/bin/celery -A config beat --loglevel=INFO
```

Run verification:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/ruff check .
.venv/bin/ruff format --check config dashboard library enrichment playlists tests
.venv/bin/pytest
```

## Debian 13 Proxmox LXC

The installer expects a fresh Debian 13 container with working DNS, a domain pointing to it, ports 80/443 forwarded, and the audio library mounted into the LXC. Run as root:

```bash
./scripts/install-lxc.sh music.example.com admin@example.com
```

The script clones `https://github.com/seanbrown-com/kuratorr.git` automatically. It installs PostgreSQL, Redis, Nginx, Python, Gunicorn, Celery, and systemd services; generates database, Django, and one-time setup secrets; runs migrations/static collection; and prints the setup token. Supplying an email enables automatic Let's Encrypt HTTPS; omitting it creates an HTTP installation suitable for a trusted LAN. Add API credentials on the Settings page (or use environment fallbacks), make mounted paths readable by `kuratorr`, make output paths writable, and restart the three application services after environment changes.

Update an installed service as root:

```bash
/opt/kuratorr/scripts/update-from-git.sh
```

The updater logs and times each stage, creates a verbose timestamped PostgreSQL backup, requires a non-interactive fast-forward Git pull, installs dependency changes, migrates, collects static files, restarts services, and displays their status.

See [architecture](docs/architecture.md), [data-source behavior](docs/data-sources.md), and the [deployment runbook](docs/deployment.md).

## Important operational notes

- Only files accessible inside the service/container can be scanned or copied.
- Downloaded copy scripts take `SOURCE_DIR` and `DESTINATION_DIR`; downloaded M3Us use the full server paths stored for their tracks.
- A normal deletion and a permanently suppressed deletion both remain restorable. Permanent means generation skips that exact definition until restoration.
- External matching is intentionally conservative. Rejected evidence is preserved as provenance but never contributes to playlists.
- No artist or album imagery is downloaded or displayed.
