# Architecture

## Components

- **Django/Gunicorn:** authenticated operator UI, configuration, review, and exports.
- **PostgreSQL:** canonical library entities, source observations, job history, review state, and playlist snapshots.
- **Redis/Celery:** durable asynchronous scans, enrichment, playlist generation, and scheduled continuation.
- **Celery Beat:** periodically queues a bounded number of artists whose configured sources have not yet been attempted. Failed sources remain visible and require a manual retry instead of consuming API quota forever.
- **Nginx:** public TLS termination, static files, proxy headers, and request-size limits.

## Data boundaries

`library` contains local/canonical entities: `LibraryRoot`, `Artist`, `ArtistAlias`, `Album`, `Track`, `Genre`, selected `AlbumGenre`, `ScanIssue`, and singleton `ServiceSettings`. Provider credentials saved through the operator UI are encrypted using a key derived from Django's service secret; environment variables remain fallback inputs.

`enrichment` keeps provenance separate: `SourceRecord` stores the original JSON/parsed payload; `ExternalIdentifier` links a source identity to a local entity; `ExternalTrack` stores a source track candidate and local match; `NoteworthyEvidence`, `AlbumGenreEvidence`, and `RelatedArtistEvidence` store independent claims with confidence and review decision. `JobRun` is the audit trail for asynchronous operations.

`playlists` stores a stable playlist definition and an ordered snapshot of tracks. `definition_key` makes generation idempotent. A deleted row is never silently recreated; `never_regenerate` distinguishes an explicit permanent suppression while still allowing an administrator to restore it.

## Trust and merge rules

1. Local raw tags are preserved exactly in `Track.raw_metadata`.
2. External source payloads are never merged destructively.
3. Normalized names exist only to search and match; display values remain intact.
4. A fuzzy track score at or above 0.90 is auto-accepted; scores below 0.72 are not linked; the middle range is reviewable.
5. YouTube additionally requires an official-video confidence threshold and excludes known non-video formats.
6. Accepted evidence contributes to playlists. Pending/rejected evidence does not.
7. Manual album genres take precedence over automatic genre selection.

## Scan semantics

Scans recurse only into `.mp3` and `.flac` files. Unchanged size and nanosecond modification time avoid reparsing. Changed files update their canonical row. Files absent from a complete scan are retained but marked unavailable, preserving playlist history and source matches. Malformed files create/update `ScanIssue`; a later successful read marks the issue resolved.

## Job semantics

Jobs progress through queued, running, succeeded, failed, or cancelled states. A library scan queues a full enrichment run after successful completion. Full enrichment isolates source failures per artist and queues playlist generation after all artists have been attempted. All pipeline operations can also be started manually.

Celery tasks are idempotent at their database boundaries: local files use `update_or_create`, source records use source/kind/external-ID uniqueness, evidence has source-specific uniqueness, and playlists use stable definition keys.

## Playlist semantics

- Best of Artist has no one-hour requirement, because a small local catalog should still produce an artist playlist.
- Year, decade, genre, combined genre/time, and radio playlists require the configured minimum duration.
- Year uses the track year and falls back to album year.
- Album genre assignments—not artist-wide genres—drive genre playlists.
- Radio alternates the seed artist's accepted hits with accepted related local artists' hits.
- M3U materialization writes a temporary file and atomically replaces the destination.
