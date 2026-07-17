from django.contrib import admin

from playlists.models import Playlist, PlaylistOutputRoot, PlaylistTrack


class PlaylistTrackInline(admin.TabularInline):
    model = PlaylistTrack
    extra = 0


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "playlist_type",
        "track_count",
        "duration_seconds",
        "deleted_at",
        "never_regenerate",
    ]
    list_filter = ["playlist_type", "never_regenerate"]
    search_fields = ["name"]
    inlines = [PlaylistTrackInline]


admin.site.register(PlaylistOutputRoot)
