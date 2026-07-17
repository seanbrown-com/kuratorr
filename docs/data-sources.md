# Data sources and matching

## Spotify

Uses Client Credentials, artist search, and the artist top-tracks endpoint. Rank and the currently returned popularity value are stored independently. Spotify does not expose public raw track play counts, and popularity is deprecated, so it must not be interpreted as a count. Configure the maximum rank depth and market in Service Settings.

Official reference: <https://developer.spotify.com/documentation/web-api/reference/get-an-artists-top-tracks>

## Last.fm

Uses `artist.getTopTracks` for source-specific play counts and `artist.getSimilar` for radio relationships. Configure minimum play count and maximum returned tracks. A Last.fm API key is required, but these reads do not require user authentication.

Official reference: <https://www.last.fm/api/show/artist.getTopTracks>

## MusicBrainz

Matches artists, browses release groups, stores MBIDs, gathers release-group genres/tags, and reads artist relationships. Release-group evidence is matched to local albums instead of applying a timeless artist-wide genre.

MusicBrainz permits an average of one request per second per source IP. Kuratorr coordinates this limit across all Celery worker processes with a Redis lock and timestamp, rather than relying on a process-local delay. If Redis is temporarily unavailable, the client falls back to a local limiter and continues using bounded retries.

Official reference: <https://musicbrainz.org/doc/MusicBrainz_API>

## Wikipedia

Uses the MediaWiki API to search and obtain parsed page HTML. It examines sections named Singles, Music videos, or Videography, accepting both tables and lists. Because page structure is community-authored and inconsistent, extracted mentions still pass through local fuzzy matching and review state. The legacy parser in `legacy/` and the sibling `wiki_music_scraper` project informed this implementation, but are not runtime dependencies.

Official reference: <https://www.mediawiki.org/wiki/API:Main_page>

## YouTube

Uses YouTube Data API search and video-detail endpoints. A candidate must avoid lyric/audio/visualizer/fan terms and gain confidence from explicit “official music video” language plus an artist/VEVO-like channel. Strong candidates with strong local matches are auto-accepted; everything else remains pending.

The API does not expose a definitive public `official_music_video` boolean, so this classification is a documented heuristic rather than a fact from YouTube.

Official reference: <https://developers.google.com/youtube/v3/docs/search/list>

## Future related-artist evidence

The schema supports additional source-specific relationship types. Good future candidates are setlist.fm for co-billing/touring evidence and Discogs for credits and membership, subject to API terms, rate limits, and separate provenance. They are not called by the current implementation.
