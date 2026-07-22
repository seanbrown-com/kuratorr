from django.db import models

from library.models import Album, Artist, Genre, TimestampedModel, Track


class Source(models.TextChoices):
    LOCAL = "local", "Local tags"
    MUSICBRAINZ = "musicbrainz", "MusicBrainz"
    SPOTIFY = "spotify", "Spotify"
    LASTFM = "lastfm", "Last.fm"
    WIKIPEDIA = "wikipedia", "Wikipedia"
    YOUTUBE = "youtube", "YouTube"


class Decision(models.TextChoices):
    PENDING = "pending", "Pending review"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class JobRun(TimestampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    job_type = models.CharField(max_length=80, db_index=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="child_jobs",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    celery_task_id = models.CharField(max_length=255, blank=True)
    requested_manually = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    progress_current = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=0)
    current_item = models.CharField(max_length=4096, blank=True)
    summary = models.JSONField(default=dict)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class ArtistSourceStatus(TimestampedModel):
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="source_statuses")
    source = models.CharField(max_length=30, choices=Source.choices)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    last_succeeded_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["artist", "source"], name="unique_artist_source_status")
        ]


class SourceRecord(TimestampedModel):
    source = models.CharField(max_length=30, choices=Source.choices, db_index=True)
    entity_kind = models.CharField(max_length=40, db_index=True)
    external_id = models.CharField(max_length=1000)
    canonical_url = models.URLField(max_length=2000, blank=True)
    fetched_at = models.DateTimeField()
    payload = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "entity_kind", "external_id"], name="unique_source_record"
            )
        ]


class ExternalIdentifier(TimestampedModel):
    source = models.CharField(max_length=30, choices=Source.choices)
    entity_kind = models.CharField(max_length=20)
    external_id = models.CharField(max_length=1000)
    artist = models.ForeignKey(
        Artist, null=True, blank=True, on_delete=models.CASCADE, related_name="external_ids"
    )
    album = models.ForeignKey(
        Album, null=True, blank=True, on_delete=models.CASCADE, related_name="external_ids"
    )
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.CASCADE, related_name="external_ids"
    )
    source_record = models.ForeignKey(
        SourceRecord, null=True, blank=True, on_delete=models.SET_NULL
    )
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=1)
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.ACCEPTED)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "entity_kind", "external_id"], name="unique_external_identifier"
            )
        ]


class ExternalTrack(TimestampedModel):
    source_record = models.OneToOneField(
        SourceRecord, on_delete=models.CASCADE, related_name="external_track"
    )
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="external_tracks")
    matched_track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.SET_NULL, related_name="external_matches"
    )
    artist_name = models.CharField(max_length=500)
    title = models.CharField(max_length=1000)
    album_title = models.CharField(max_length=700, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_seconds = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    rank = models.PositiveIntegerField(null=True, blank=True)
    playcount = models.PositiveBigIntegerField(null=True, blank=True)
    popularity = models.PositiveSmallIntegerField(null=True, blank=True)
    match_confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    match_decision = models.CharField(
        max_length=20, choices=Decision.choices, default=Decision.PENDING
    )


class NoteworthyEvidence(TimestampedModel):
    class EvidenceType(models.TextChoices):
        SPOTIFY_TOP = "spotify_top", "Spotify top track"
        LASTFM_TOP = "lastfm_top", "Last.fm top track"
        WIKIPEDIA_SINGLE = "wikipedia_single", "Wikipedia single"
        WIKIPEDIA_VIDEO = "wikipedia_video", "Wikipedia music video"
        YOUTUBE_OFFICIAL = "youtube_official", "YouTube official music video"
        MANUAL = "manual", "Manual"

    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="noteworthy_evidence")
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.CASCADE, related_name="noteworthy_evidence"
    )
    external_track = models.ForeignKey(
        ExternalTrack, null=True, blank=True, on_delete=models.CASCADE, related_name="evidence"
    )
    evidence_type = models.CharField(max_length=40, choices=EvidenceType.choices)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    decision = models.CharField(
        max_length=20, choices=Decision.choices, default=Decision.PENDING, db_index=True
    )
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["external_track", "evidence_type"], name="unique_external_track_evidence"
            )
        ]


class AlbumGenreEvidence(TimestampedModel):
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="genre_evidence")
    genre = models.ForeignKey(Genre, on_delete=models.PROTECT, related_name="album_evidence")
    source_record = models.ForeignKey(
        SourceRecord, null=True, blank=True, on_delete=models.SET_NULL
    )
    source = models.CharField(max_length=30, choices=Source.choices)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0.5)
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.ACCEPTED)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["album", "genre", "source"], name="unique_album_genre_evidence"
            )
        ]


class RelatedArtistEvidence(TimestampedModel):
    class RelationshipType(models.TextChoices):
        SIMILAR = "similar", "Similar artist"
        MEMBER_OF = "member_of", "Shares members"
        TOURING = "touring", "Toured together"
        COLLABORATOR = "collaborator", "Collaborator"

    artist = models.ForeignKey(
        Artist, on_delete=models.CASCADE, related_name="outgoing_relationships"
    )
    related_artist = models.ForeignKey(
        Artist,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="incoming_relationships",
    )
    related_artist_name = models.CharField(max_length=500)
    relationship_type = models.CharField(max_length=30, choices=RelationshipType.choices)
    source_record = models.ForeignKey(
        SourceRecord, null=True, blank=True, on_delete=models.SET_NULL
    )
    source = models.CharField(max_length=30, choices=Source.choices)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0.5)
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.PENDING)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["artist", "related_artist_name", "relationship_type", "source"],
                name="unique_related_artist_evidence",
            )
        ]


class ArtistRecommendation(TimestampedModel):
    """Materialized ranking of discovered artists absent from the local library."""

    name = models.CharField(max_length=500)
    normalized_name = models.CharField(max_length=500, unique=True)
    rank = models.PositiveIntegerField(db_index=True)
    linked_artist_count = models.PositiveIntegerField(default=0)
    evidence_count = models.PositiveIntegerField(default=0)
    linked_artists = models.JSONField(default=list)
    sources = models.JSONField(default=list)
    relationship_types = models.JSONField(default=list)

    class Meta:
        ordering = ["rank", "name"]


class MissingAlbum(TimestampedModel):
    """An album release reported by a source but absent from the local library."""

    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="missing_albums")
    source = models.CharField(max_length=30, choices=Source.choices)
    source_record = models.ForeignKey(SourceRecord, on_delete=models.CASCADE)
    external_id = models.CharField(max_length=1000)
    title = models.CharField(max_length=700)
    normalized_title = models.CharField(max_length=700, db_index=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    release_type = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["artist__name", "year", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["artist", "source", "external_id"], name="unique_missing_album"
            )
        ]
