import os
import threading
import time
from urllib.parse import quote

import requests
from django.conf import settings
from redis import Redis
from redis.exceptions import RedisError

from library.models import ServiceSettings


class ApiError(RuntimeError):
    pass


class ProviderNotConfigured(ApiError):
    pass


class BaseClient:
    max_attempts = 4
    retry_statuses = {429, 500, 502, 503, 504}

    def __init__(self):
        self.session = requests.Session()
        service_settings = ServiceSettings.load()
        user_agent = service_settings.http_user_agent or os.getenv(
            "HTTP_USER_AGENT", "Kuratorr/1.0 (https://github.com/seanbrown-com/kuratorr)"
        )
        self.session.headers.update({"User-Agent": user_agent})

    def json(self, method, url, **kwargs):
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # Do not reuse a connection pool after a TLS/socket failure.
                for adapter in self.session.adapters.values():
                    adapter.close()
                if attempt == self.max_attempts:
                    raise ApiError(
                        f"Request to {url} failed after {self.max_attempts} attempts: {exc}"
                    ) from exc
                delay = min(2 ** (attempt - 1), 8)
            else:
                if response.ok:
                    return response.json()
                if response.status_code not in self.retry_statuses or attempt == self.max_attempts:
                    raise ApiError(f"{response.status_code} from {url}: {response.text[:300]}")
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = min(2 ** (attempt - 1), 8)
            time.sleep(max(delay, 1.0))
        raise ApiError(f"Request to {url} failed")


class SpotifyClient(BaseClient):
    def __init__(self):
        super().__init__()
        service_settings = ServiceSettings.load()
        client_id = service_settings.provider_value("spotify_client_id", "SPOTIFY_CLIENT_ID")
        client_secret = service_settings.provider_value(
            "spotify_client_secret", "SPOTIFY_CLIENT_SECRET"
        )
        if not client_id or not client_secret:
            raise ProviderNotConfigured("Spotify credentials are not configured in Settings")
        token = self.json(
            "POST",
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
        )["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"

    def find_artist(self, name):
        data = self.json(
            "GET",
            "https://api.spotify.com/v1/search",
            params={"q": f'artist:"{name}"', "type": "artist", "limit": 10},
        )
        return data.get("artists", {}).get("items", [])

    def top_tracks(self, artist_id, market="US"):
        data = self.json(
            "GET",
            f"https://api.spotify.com/v1/artists/{artist_id}/top-tracks",
            params={"market": market},
        )
        return data.get("tracks", [])


class LastFmClient(BaseClient):
    endpoint = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self):
        super().__init__()
        self.api_key = ServiceSettings.load().provider_value("lastfm_api_key", "LASTFM_API_KEY")
        if not self.api_key:
            raise ProviderNotConfigured("Last.fm API key is not configured in Settings")

    def artist_top_tracks(self, artist, limit=50):
        data = self.json(
            "GET",
            self.endpoint,
            params={
                "method": "artist.gettoptracks",
                "artist": artist,
                "limit": limit,
                "api_key": self.api_key,
                "format": "json",
                "autocorrect": 1,
            },
        )
        return data.get("toptracks", {}).get("track", [])

    def similar_artists(self, artist, limit=30):
        data = self.json(
            "GET",
            self.endpoint,
            params={
                "method": "artist.getsimilar",
                "artist": artist,
                "limit": limit,
                "api_key": self.api_key,
                "format": "json",
                "autocorrect": 1,
            },
        )
        return data.get("similarartists", {}).get("artist", [])


class MusicBrainzClient(BaseClient):
    endpoint = "https://musicbrainz.org/ws/2"
    request_lock = threading.Lock()
    last_request_at = 0.0
    rate_redis = None
    rate_lock_name = "kuratorr:musicbrainz:request-lock"
    rate_timestamp_name = "kuratorr:musicbrainz:last-request-at"

    def __init__(self):
        super().__init__()

    @classmethod
    def _redis(cls):
        if cls.rate_redis is None:
            cls.rate_redis = Redis.from_url(
                settings.CELERY_BROKER_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
        return cls.rate_redis

    def _locally_limited_json(self, method, url, **kwargs):
        """Fallback used only when Redis itself is unavailable."""
        with self.request_lock:
            wait = 1.1 - (time.monotonic() - type(self).last_request_at)
            if wait > 0:
                time.sleep(wait)
            try:
                return super().json(method, url, **kwargs)
            finally:
                type(self).last_request_at = time.monotonic()

    def json(self, method, url, **kwargs):
        """Globally limit MusicBrainz across all Celery worker processes."""
        try:
            redis = self._redis()
            lock = redis.lock(
                self.rate_lock_name,
                timeout=180,
                blocking_timeout=240,
            )
            acquired = lock.acquire(blocking=True)
        except RedisError:
            return self._locally_limited_json(method, url, **kwargs)
        if not acquired:
            raise ApiError("Timed out waiting for the shared MusicBrainz request limit")
        try:
            try:
                last_request_at = float(redis.get(self.rate_timestamp_name) or 0)
            except (RedisError, TypeError, ValueError):
                last_request_at = 0
            wait = 1.1 - (time.time() - last_request_at)
            if wait > 0:
                time.sleep(wait)
            return super().json(method, url, **kwargs)
        finally:
            try:
                redis.set(self.rate_timestamp_name, time.time(), ex=300)
                lock.release()
            except RedisError:
                pass

    def find_artist(self, name):
        return self.json(
            "GET",
            f"{self.endpoint}/artist/",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": 10},
        ).get("artists", [])

    def release_groups(self, artist_mbid):
        result = []
        offset = 0
        while True:
            data = self.json(
                "GET",
                f"{self.endpoint}/release-group",
                params={
                    "artist": artist_mbid,
                    "fmt": "json",
                    "limit": 100,
                    "offset": offset,
                    "inc": "genres+tags",
                },
            )
            batch = data.get("release-groups", [])
            result.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        return result

    def relationships(self, artist_mbid):
        return self.json(
            "GET",
            f"{self.endpoint}/artist/{artist_mbid}",
            params={"fmt": "json", "inc": "artist-rels"},
        ).get("relations", [])


class WikipediaClient(BaseClient):
    endpoint = "https://en.wikipedia.org/w/api.php"

    def find_page(self, artist):
        data = self.json(
            "GET",
            self.endpoint,
            params={
                "action": "query",
                "list": "search",
                "srsearch": f"{artist} musical artist",
                "srlimit": 10,
                "format": "json",
                "formatversion": 2,
            },
        )
        return data.get("query", {}).get("search", [])

    def page_html(self, title):
        data = self.json(
            "GET",
            self.endpoint,
            params={
                "action": "parse",
                "page": title,
                "prop": "text|sections",
                "format": "json",
                "formatversion": 2,
            },
        )
        return data.get("parse", {})


class YouTubeClient(BaseClient):
    endpoint = "https://www.googleapis.com/youtube/v3"

    def __init__(self):
        super().__init__()
        self.api_key = ServiceSettings.load().provider_value("youtube_api_key", "YOUTUBE_API_KEY")
        if not self.api_key:
            raise ProviderNotConfigured("YouTube API key is not configured in Settings")

    def search_official_videos(self, artist, max_results=25):
        data = self.json(
            "GET",
            f"{self.endpoint}/search",
            params={
                "key": self.api_key,
                "part": "snippet",
                "type": "video",
                "q": f'{artist} "official music video"',
                "maxResults": max_results,
                "videoCategoryId": "10",
            },
        )
        ids = [
            item["id"]["videoId"]
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        if not ids:
            return []
        details = self.json(
            "GET",
            f"{self.endpoint}/videos",
            params={
                "key": self.api_key,
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(ids),
            },
        )
        return details.get("items", [])


def wikipedia_url(title):
    return f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
