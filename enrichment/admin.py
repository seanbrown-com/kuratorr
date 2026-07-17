from django.contrib import admin

from enrichment.models import (
    AlbumGenreEvidence,
    ArtistRecommendation,
    ArtistSourceStatus,
    ExternalIdentifier,
    ExternalTrack,
    JobRun,
    NoteworthyEvidence,
    RelatedArtistEvidence,
    SourceRecord,
)


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    list_display = ["job_type", "status", "requested_manually", "created_at", "finished_at"]
    list_filter = ["status", "job_type", "requested_manually"]


@admin.register(NoteworthyEvidence)
class EvidenceAdmin(admin.ModelAdmin):
    list_display = ["artist", "track", "evidence_type", "confidence", "decision"]
    list_filter = ["evidence_type", "decision"]
    search_fields = ["artist__name", "track__title", "external_track__title"]


admin.site.register(SourceRecord)
admin.site.register(ExternalIdentifier)
admin.site.register(ExternalTrack)
admin.site.register(AlbumGenreEvidence)
admin.site.register(RelatedArtistEvidence)
admin.site.register(ArtistSourceStatus)
admin.site.register(ArtistRecommendation)
