import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ServiceSettings(TimestampedModel):
    spotify_max_tracks = models.PositiveSmallIntegerField(default=20)
    spotify_noteworthy_max_rank = models.PositiveSmallIntegerField(default=2)
    lastfm_min_playcount = models.PositiveBigIntegerField(default=1000)
    lastfm_max_tracks = models.PositiveSmallIntegerField(default=50)
    lastfm_noteworthy_max_rank = models.PositiveSmallIntegerField(default=2)
    minimum_playlist_seconds = models.PositiveIntegerField(default=3600)
    max_album_genres = models.PositiveSmallIntegerField(default=3)
    spotify_market = models.CharField(max_length=2, default="US")
    youtube_max_results = models.PositiveSmallIntegerField(default=25)
    youtube_auto_accept_confidence = models.DecimalField(
        max_digits=4, decimal_places=3, default=0.9
    )
    spotify_client_id_encrypted = models.TextField(blank=True)
    spotify_client_secret_encrypted = models.TextField(blank=True)
    lastfm_api_key_encrypted = models.TextField(blank=True)
    youtube_api_key_encrypted = models.TextField(blank=True)
    http_user_agent = models.CharField(
        max_length=500,
        blank=True,
        help_text="Identify this service with a contact email or URL for MusicBrainz/Wikimedia.",
    )

    SECRET_FIELDS = {
        "spotify_client_id": "spotify_client_id_encrypted",
        "spotify_client_secret": "spotify_client_secret_encrypted",
        "lastfm_api_key": "lastfm_api_key_encrypted",
        "youtube_api_key": "youtube_api_key_encrypted",
    }

    @staticmethod
    def _credential_cipher():
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    def set_secret(self, name, value):
        field = self.SECRET_FIELDS[name]
        encrypted = self._credential_cipher().encrypt(value.encode()).decode() if value else ""
        setattr(self, field, encrypted)

    def get_secret(self, name):
        encrypted = getattr(self, self.SECRET_FIELDS[name], "")
        if not encrypted:
            return ""
        try:
            return self._credential_cipher().decrypt(encrypted.encode()).decode()
        except (InvalidToken, ValueError):
            return ""

    def provider_value(self, name, environment_name):
        return self.get_secret(name) or os.getenv(environment_name, "")

    @property
    def spotify_configured(self):
        return bool(
            self.provider_value("spotify_client_id", "SPOTIFY_CLIENT_ID")
            and self.provider_value("spotify_client_secret", "SPOTIFY_CLIENT_SECRET")
        )

    @property
    def lastfm_configured(self):
        return bool(self.provider_value("lastfm_api_key", "LASTFM_API_KEY"))

    @property
    def youtube_configured(self):
        return bool(self.provider_value("youtube_api_key", "YOUTUBE_API_KEY"))

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    def __str__(self):
        return "Service settings"


class LibraryRoot(TimestampedModel):
    path = models.CharField(max_length=2048, unique=True)
    enabled = models.BooleanField(default=True)
    last_scanned_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if not Path(self.path).is_absolute():
            raise ValidationError({"path": "Library roots must be absolute paths."})

    def __str__(self):
        return self.path


class Artist(TimestampedModel):
    name = models.CharField(max_length=500)
    sort_name = models.CharField(max_length=500, blank=True)
    normalized_name = models.CharField(max_length=500, unique=True, db_index=True)
    is_reviewed = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ArtistAlias(TimestampedModel):
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField(max_length=500)
    normalized_name = models.CharField(max_length=500, db_index=True)
    source = models.CharField(max_length=40, default="local")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["artist", "normalized_name"], name="unique_artist_alias"
            )
        ]


class Album(TimestampedModel):
    class AlbumType(models.TextChoices):
        ALBUM = "album", "Album"
        EP = "ep", "EP"
        SINGLE = "single", "Single"
        COMPILATION = "compilation", "Compilation"
        UNKNOWN = "unknown", "Unknown"

    artist = models.ForeignKey(Artist, on_delete=models.PROTECT, related_name="albums")
    title = models.CharField(max_length=700)
    normalized_title = models.CharField(max_length=700, db_index=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    album_type = models.CharField(
        max_length=20, choices=AlbumType.choices, default=AlbumType.UNKNOWN
    )
    is_reviewed = models.BooleanField(default=False)

    class Meta:
        ordering = ["artist__name", "year", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["artist", "normalized_title", "year"], name="unique_local_album"
            )
        ]

    def __str__(self):
        return f"{self.artist} — {self.title}"


class Genre(TimestampedModel):
    name = models.CharField(max_length=120, unique=True)
    normalized_name = models.CharField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AlbumGenre(TimestampedModel):
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="genre_assignments")
    genre = models.ForeignKey(Genre, on_delete=models.PROTECT, related_name="album_assignments")
    rank = models.PositiveSmallIntegerField(default=1)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=1)
    is_manual = models.BooleanField(default=False)

    class Meta:
        ordering = ["rank"]
        constraints = [
            models.UniqueConstraint(fields=["album", "genre"], name="unique_album_genre"),
            models.UniqueConstraint(fields=["album", "rank"], name="unique_album_genre_rank"),
        ]


class Track(TimestampedModel):
    class AudioFormat(models.TextChoices):
        MP3 = "mp3", "MP3"
        FLAC = "flac", "FLAC"

    library_root = models.ForeignKey(LibraryRoot, on_delete=models.PROTECT, related_name="tracks")
    artist = models.ForeignKey(Artist, on_delete=models.PROTECT, related_name="tracks")
    album = models.ForeignKey(Album, on_delete=models.PROTECT, related_name="tracks")
    full_path = models.CharField(max_length=4096, unique=True)
    relative_path = models.CharField(max_length=4096)
    file_format = models.CharField(max_length=10, choices=AudioFormat.choices)
    title = models.CharField(max_length=1000)
    normalized_title = models.CharField(max_length=1000, db_index=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    track_number = models.PositiveSmallIntegerField(null=True, blank=True)
    disc_number = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_seconds = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    bitrate = models.PositiveIntegerField(null=True, blank=True)
    sample_rate = models.PositiveIntegerField(null=True, blank=True)
    channels = models.PositiveSmallIntegerField(null=True, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    file_modified_ns = models.PositiveBigIntegerField(default=0)
    raw_metadata = models.JSONField(default=dict)
    is_available = models.BooleanField(default=True, db_index=True)
    scan_error = models.TextField(blank=True)

    class Meta:
        ordering = [
            "artist__name",
            "album__year",
            "album__title",
            "disc_number",
            "track_number",
            "title",
        ]
        indexes = [models.Index(fields=["artist", "normalized_title"])]

    def __str__(self):
        return f"{self.artist} — {self.title}"


class ScanIssue(TimestampedModel):
    library_root = models.ForeignKey(
        LibraryRoot, on_delete=models.CASCADE, related_name="scan_issues"
    )
    full_path = models.CharField(max_length=4096)
    error = models.TextField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["library_root", "full_path"], name="unique_scan_issue")
        ]
