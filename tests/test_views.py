from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from enrichment.models import ArtistRecommendation, Decision, NoteworthyEvidence
from library.models import LibraryRoot, ServiceSettings
from playlists.models import Playlist
from playlists.services import generate_artist_playlists

TEST_STORAGES = {
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
}


@pytest.mark.django_db
@override_settings(INITIAL_SETUP_TOKEN="secret-token", STORAGES=TEST_STORAGES)
def test_initial_setup_requires_token_and_creates_only_admin(client):
    response = client.post(
        reverse("initial-setup"),
        {
            "token": "wrong",
            "username": "admin",
            "password1": "Very-Long-Test-Passphrase!",
            "password2": "Very-Long-Test-Passphrase!",
        },
    )
    assert response.status_code == 200
    assert get_user_model().objects.count() == 0
    response = client.post(
        reverse("initial-setup"),
        {
            "token": "secret-token",
            "username": "admin",
            "password1": "Very-Long-Test-Passphrase!",
            "password2": "Very-Long-Test-Passphrase!",
        },
    )
    assert response.status_code == 302
    user = get_user_model().objects.get()
    assert user.is_superuser and user.is_staff


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_anonymous_user_redirected_to_login(client, django_user_model):
    django_user_model.objects.create_superuser("admin", password="Very-Long-Test-Passphrase!")
    response = client.get(reverse("track-list"))
    assert response.status_code == 302
    assert "/login/" in response.url


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_track_list_shows_required_metadata(client, django_user_model, track):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    client.force_login(user)
    response = client.get(reverse("track-list"))
    body = response.content.decode()
    assert response.status_code == 200
    assert all(value in body for value in ("Deftones", "Change", "White Pony", "2000"))


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_playlist_downloads_use_external_source_and_bulk_zip(
    client, django_user_model, track, artist
):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    NoteworthyEvidence.objects.create(
        artist=artist,
        track=track,
        evidence_type=NoteworthyEvidence.EvidenceType.MANUAL,
        confidence=Decimal("1"),
        decision=Decision.ACCEPTED,
    )
    generate_artist_playlists()
    playlist = Playlist.objects.get()
    client.force_login(user)

    assert client.get(reverse("download-m3u", args=[playlist.pk])).status_code == 405
    response = client.post(
        reverse("download-m3u", args=[playlist.pk]),
        {"source_directory": "/media/music"},
    )
    assert response.status_code == 200
    assert b"/media/music/Change.mp3" in response.content
    assert track.full_path.encode() not in response.content

    response = client.get(reverse("download-copy-script", args=[playlist.pk]))
    assert b"SOURCE_DIR DESTINATION_DIR" in response.content
    assert track.full_path.encode() not in response.content

    response = client.post(
        reverse("download-all-m3u"), {"source_directory": "/media/music"}
    )
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    with ZipFile(BytesIO(response.content)) as archive:
        assert b"/media/music/Change.mp3" in archive.read("Best_of_Deftones.m3u")


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_library_root_page_browses_service_directories(client, django_user_model, tmp_path):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    child = tmp_path / "Albums"
    child.mkdir()
    (tmp_path / "not-a-directory.mp3").write_bytes(b"")
    client.force_login(user)

    response = client.get(reverse("root-list"), {"browse": str(tmp_path)})
    body = response.content.decode()

    assert response.status_code == 200
    assert "Albums" in body
    assert "not-a-directory.mp3" not in body
    assert f'value="{tmp_path}"' in body


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_library_root_page_adds_selected_directory(client, django_user_model, tmp_path):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    client.force_login(user)

    response = client.post(
        reverse("root-list"), {"path": str(tmp_path), "enabled": "on"}
    )

    assert response.status_code == 302
    assert LibraryRoot.objects.filter(path=str(tmp_path), enabled=True).exists()


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_recommendations_page_shows_rank_and_connections(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    ArtistRecommendation.objects.create(
        name="Failure",
        normalized_name="failure",
        rank=1,
        linked_artist_count=2,
        evidence_count=3,
        linked_artists=["Deftones", "Mastodon"],
        sources=["lastfm", "wikipedia"],
    )
    client.force_login(user)

    response = client.get(reverse("artist-recommendations"))
    body = response.content.decode()

    assert response.status_code == 200
    assert all(value in body for value in ("#1", "Failure", "Deftones, Mastodon"))


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES, SECRET_KEY="credential-test-secret")
def test_settings_page_encrypts_api_credentials(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    client.force_login(user)
    response = client.post(
        reverse("settings"),
        {
            "spotify_max_tracks": 20,
            "spotify_noteworthy_max_rank": 2,
            "lastfm_min_playcount": 1000,
            "lastfm_max_tracks": 50,
            "lastfm_noteworthy_max_rank": 2,
            "minimum_playlist_seconds": 3600,
            "max_album_genres": 3,
            "spotify_market": "US",
            "youtube_max_results": 25,
            "youtube_auto_accept_confidence": "0.900",
            "http_user_agent": "Kuratorr/1.0 (admin@example.com)",
            "spotify_client_id": "spotify-id",
            "spotify_client_secret": "spotify-secret",
            "lastfm_api_key": "lastfm-key",
            "youtube_api_key": "youtube-key",
        },
    )

    assert response.status_code == 302
    settings = ServiceSettings.load()
    assert "spotify-secret" not in settings.spotify_client_secret_encrypted
    assert settings.get_secret("spotify_client_id") == "spotify-id"
    assert settings.get_secret("spotify_client_secret") == "spotify-secret"
    assert settings.get_secret("lastfm_api_key") == "lastfm-key"
    assert settings.get_secret("youtube_api_key") == "youtube-key"
