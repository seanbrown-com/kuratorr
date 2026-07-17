from django.urls import path

from enrichment import views

urlpatterns = [
    path("recommendations/", views.recommendations, name="artist-recommendations"),
    path("missing/", views.missing_albums, name="missing-albums"),
    path("review/", views.review_queue, name="review-queue"),
    path("review/<int:pk>/<str:decision>/", views.review_evidence, name="review-evidence"),
    path(
        "artists/<int:artist_id>/<str:source>/run/",
        views.run_artist_source,
        name="run-artist-source",
    ),
]
