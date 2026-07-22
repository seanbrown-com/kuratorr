# Architecture

## Components

- **Django/Gunicorn:** authenticated operator UI, configuration, review, and exports.
- **PostgreSQL:** canonical library entities, source observations, job history, review state, and playlist snapshots.
- **Redis/Celery:** durable asynchronous scans, enrichment, playlist generation, and scheduled continuation.
- **Celery Beat:** periodically queues a bounded number of artists whose configured sources have not yet been attempted and requeues failed provider work only after its stored retry time.
- **Nginx:** public TLS termination, static files, proxy headers, and request-size limits.

## Data boundaries

`library` contains local/canonical entities: `LibraryRoot`, `Artist`, `ArtistAlias`, `Album`, `Track`, `Genre`, selected `AlbumGenre`, `ScanIssue`, and singleton `ServiceSettings`. Provider credentials saved through the operator UI are encrypted using a key derived from Django's service secret; environment variables remain fallback inputs.

`enrichment` keeps provenance separate: `SourceRecord` stores the original JSON/parsed payload; `ExternalIdentifier` links a source identity to a local entity; `ExternalTrack` stores a source track candidate and local match; `NoteworthyEvidence`, `AlbumGenreEvidence`, and `RelatedArtistEvidence` store independent claims with confidence and review decision. `JobRun` is the audit trail for asynchronous operations.

`playlists` stores a stable playlist definition and an ordered snapshot of tracks. `definition_key` makes generation idempotent. A deleted row is never silently recreated; `never_regenerate` distinguishes an explicit permanent suppression while still allowing an administrator to restore it.

## Trust and merge rules

1. Kuratorr stores only the normalized file metadata used by the application. Source audio files remain authoritative and can be rescanned if the application needs additional fields later; unused tags and embedded artwork are not persisted.
2. External source payloads are never merged destructively.
3. Normalized names exist only to search and match; display values remain intact.
4. Track matching compares whole normalized titles after removing common edition suffixes. A score at or above the configurable auto-accept threshold (default 0.95) is accepted, scores from the Review threshold (default 0.85) enter Review, and lower scores are rejected automatically.
5. YouTube additionally requires an official-video confidence threshold and excludes known non-video formats.
6. Accepted evidence contributes to playlists. Pending/rejected evidence does not.

Wikipedia table/list parsing removes rendered citation nodes and trailing reference markers before storing a candidate title. During reconciliation, title confidence is recalculated from the current external and local titles rather than retaining a stale historical minimum; provider/artist identity confidence is evaluated separately.

MusicBrainz album release groups that do not match a local album are materialized separately as `MissingAlbum` records. The Missing page shows only releases that can be associated with at least one source-qualified noteworthy external track and supports release-type filtering. Records are reconciled whenever MusicBrainz enrichment runs again; singles and other non-album release groups are not presented as missing albums.
7. Manual album genres take precedence over automatic genre selection.

## Scan semantics

Scans recurse only into `.mp3` and `.flac` files. Unchanged size and nanosecond modification time avoid reparsing. Changed files update their canonical row. Files absent from a complete scan are retained but marked unavailable, preserving playlist history and source matches. Malformed files create/update `ScanIssue`; a later successful read marks the issue resolved.

## Job semantics

Jobs progress through queued, running, succeeded, failed, or cancelled states. A library scan reads only local files and reports per-file progress; it never starts enrichment. Manual full-library enrichment fans out one child job per available artist, and each terminal child atomically advances the parent’s progress. The parent completes successfully only when every child succeeds. Job History can cooperatively cancel queued/running parents and children, and running jobs without a heartbeat for an hour are marked failed rather than remaining stale indefinitely.

Celery tasks are idempotent at their database boundaries: local files use `update_or_create`, source records use source/kind/external-ID uniqueness, evidence has source-specific uniqueness, and playlists use stable definition keys.

External clients use a Redis-backed provider circuit breaker. A 429 response prevents every worker from calling that provider again during the cooldown, honors `Retry-After` when supplied, and applies persistent exponential backoff per artist/source. YouTube daily-quota exhaustion opens a 24-hour cooldown. The continuation task requeues no more than five eligible failed sources per five-minute cycle and leases each queued retry for 15 minutes to prevent duplicate dispatch.

## Playlist semantics

- Best of Artist has no one-hour requirement, because a small local catalog should still produce an artist playlist.
- Year, decade, genre, combined genre/time, and radio playlists require the configured minimum duration.
- Year uses the track year and falls back to album year.
- Album genre assignments—not artist-wide genres—drive genre playlists.
- Radio alternates the seed artist's accepted hits with accepted related local artists' hits.
- M3U materialization writes a temporary file and atomically replaces the destination.
- Kuratorr supports one enabled output root. Materialized M3Us and browser ZIP entries are grouped beneath `best of artist`, `best of genres`, `best of year`, `best of decades`, `genres by year`, `genres by decade`, and `artist radio` directories.
