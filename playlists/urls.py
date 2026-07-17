from django.urls import path

from playlists import views

urlpatterns = [
    path("", views.playlist_list, name="playlist-list"),
    path("deleted/", views.deleted_list, name="deleted-playlists"),
    path("outputs/", views.output_roots, name="playlist-output-roots"),
    path("download-all/", views.download_all_m3u, name="download-all-m3u"),
    path("<uuid:pk>/", views.playlist_detail, name="playlist-detail"),
    path("<uuid:pk>/download/", views.download_m3u, name="download-m3u"),
    path("<uuid:pk>/copy-script/", views.download_copy_script, name="download-copy-script"),
    path("<uuid:pk>/delete/", views.delete_view, name="delete-playlist"),
    path("<uuid:pk>/restore/", views.restore_view, name="restore-playlist"),
]
