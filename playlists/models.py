import uuid

from django.db import models

from library.models import Artist, Genre, TimestampedModel, Track


class Playlist(TimestampedModel):
    class PlaylistType(models.TextChoices):
        ARTIST = "artist", "Best of Artist"
        YEAR = "year", "Best of Year"
        DECADE = "decade", "Best of Decade"
        GENRE = "genre", "Best of Genre"
        GENRE_YEAR = "genre_year", "Best of Genre and Year"
        GENRE_DECADE = "genre_decade", "Best of Genre and Decade"
        ARTIST_RADIO = "artist_radio", "Artist Radio"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=700)
    playlist_type = models.CharField(max_length=30, choices=PlaylistType.choices, db_index=True)
    definition_key = models.CharField(max_length=1000, unique=True)
    artist = models.ForeignKey(
        Artist, null=True, blank=True, on_delete=models.SET_NULL, related_name="playlists"
    )
    genre = models.ForeignKey(
        Genre, null=True, blank=True, on_delete=models.SET_NULL, related_name="playlists"
    )
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    decade = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    track_count = models.PositiveIntegerField(default=0)
    output_path = models.CharField(max_length=4096, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    never_regenerate = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["playlist_type", "name"]

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def __str__(self):
        return self.name


class PlaylistTrack(TimestampedModel):
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE, related_name="entries")
    track = models.ForeignKey(Track, on_delete=models.PROTECT, related_name="playlist_entries")
    position = models.PositiveIntegerField()
    rationale = models.JSONField(default=dict)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["playlist", "position"], name="unique_playlist_position"
            ),
            models.UniqueConstraint(fields=["playlist", "track"], name="unique_playlist_track"),
        ]


class PlaylistOutputRoot(TimestampedModel):
    path = models.CharField(max_length=2048, unique=True)
    enabled = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.order_by("pk").first()

    def __str__(self):
        return self.path
