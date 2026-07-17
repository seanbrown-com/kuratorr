from django.contrib import admin

from library.models import (
    Album,
    AlbumGenre,
    Artist,
    ArtistAlias,
    Genre,
    LibraryRoot,
    ScanIssue,
    ServiceSettings,
    Track,
)

admin.site.register(ServiceSettings)
admin.site.register(LibraryRoot)
admin.site.register(ArtistAlias)
admin.site.register(Genre)
admin.site.register(AlbumGenre)
admin.site.register(ScanIssue)


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    search_fields = ["name", "normalized_name"]
    list_display = ["name", "is_reviewed", "updated_at"]


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    search_fields = ["title", "artist__name"]
    list_filter = ["album_type", "year"]
    list_display = ["title", "artist", "year", "album_type"]


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    search_fields = ["title", "artist__name", "album__title", "full_path"]
    list_filter = ["file_format", "is_available", "year"]
    list_display = ["title", "artist", "album", "year", "file_format", "is_available"]
