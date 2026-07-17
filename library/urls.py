from django.urls import path

from library import views

urlpatterns = [
    path("", views.track_list, name="track-list"),
    path("artists/", views.artist_list, name="artist-list"),
    path("artists/<int:pk>/", views.artist_detail, name="artist-detail"),
    path("roots/", views.root_list, name="root-list"),
    path("roots/<int:pk>/scan/", views.scan_root, name="scan-root"),
]
