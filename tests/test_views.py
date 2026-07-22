from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from enrichment.models import (
    ArtistRecommendation,
    Decision,
    ExternalTrack,
    JobRun,
    MissingAlbum,
    NoteworthyEvidence,
    Source,
    SourceRecord,
)
from library.models import LibraryRoot, ServiceSettings, Track
from playlists.models import Playlist, PlaylistOutputRoot, PlaylistTrack
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
    assert "kuratorr-logo-horizontal-dark.svg" in body
    assert "kuratorr/brand/site.webmanifest" in body
    assert "data-theme-toggle" in body


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_artist_page_uses_searchable_table(client, django_user_model, track):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    client.force_login(user)

    response = client.get(reverse("artist-list"), {"q": "Deft"})
    body = response.content.decode()

    assert response.status_code == 200
    assert all(value in body for value in ("<table>", "Deftones", "Available tracks"))
    assert "card-list" not in body


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_dashboard_pipeline_contains_only_icon_buttons(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    client.force_login(user)

    body = client.get(reverse("dashboard")).content.decode()

    assert all(
        label in body
        for label in (
            "Configure and Scan",
            "Run all enrichment",
            "Rank recommended artists",
            "Generate playlists",
            "Write playlists to disk",
        )
    )
    assert body.count('class="pipeline-icon"') == 5
    assert "Browse 0 artist recommendations" not in body
    assert "Configure playlist output directories" not in body


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_playlist_downloads_use_server_paths_and_bulk_zip(client, django_user_model, track, artist):
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

    response = client.get(reverse("download-m3u", args=[playlist.pk]))
    assert response.status_code == 200
    assert track.full_path.encode() in response.content

    response = client.get(reverse("download-copy-script", args=[playlist.pk]))
    assert b"SOURCE_DIR DESTINATION_DIR" in response.content
    assert track.full_path.encode() not in response.content

    response = client.get(reverse("download-all-m3u"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    with ZipFile(BytesIO(b"".join(response.streaming_content))) as archive:
        assert track.full_path.encode() in archive.read("best of artist/Best_of_Deftones.m3u")


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

    response = client.post(reverse("root-list"), {"path": str(tmp_path), "enabled": "on"})

    assert response.status_code == 302
    assert LibraryRoot.objects.filter(path=str(tmp_path), enabled=True).exists()


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_library_root_page_rejects_filesystem_root(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    client.force_login(user)

    response = client.post(reverse("root-list"), {"path": "/", "enabled": "on"})

    assert response.status_code == 200
    assert b"filesystem root cannot be used" in response.content
    assert not LibraryRoot.objects.filter(path="/").exists()


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_library_root_delete_requires_confirmation_and_removes_associated_records(
    client, django_user_model, root, track, artist, album
):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    playlist = Playlist.objects.create(
        name="Best of Deftones",
        playlist_type=Playlist.PlaylistType.ARTIST,
        definition_key="artist:deftones",
        artist=artist,
    )
    PlaylistTrack.objects.create(playlist=playlist, track=track, position=1)
    client.force_login(user)

    response = client.get(reverse("delete-root", args=[root.pk]))
    assert response.status_code == 200
    assert str(root.path).encode() in response.content
    assert b"I understand this permanently deletes" in response.content

    response = client.post(reverse("delete-root", args=[root.pk]), {})
    assert response.status_code == 200
    assert LibraryRoot.objects.filter(pk=root.pk).exists()

    response = client.post(reverse("delete-root", args=[root.pk]), {"confirm": "yes"})
    assert response.status_code == 302
    assert not LibraryRoot.objects.filter(pk=root.pk).exists()
    assert not Track.objects.filter(pk=track.pk).exists()
    assert not Playlist.objects.filter(pk=playlist.pk).exists()
    assert not type(album).objects.filter(pk=album.pk).exists()
    assert not type(artist).objects.filter(pk=artist.pk).exists()


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_library_root_delete_preserves_catalog_records_used_by_another_root(
    client, django_user_model, root, track, artist, album, tmp_path
):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    second_path = tmp_path / "second-library"
    second_path.mkdir()
    second_root = LibraryRoot.objects.create(path=str(second_path))
    second_track = Track.objects.create(
        library_root=second_root,
        artist=artist,
        album=album,
        full_path=str(second_path / "Digital Bath.mp3"),
        relative_path="Digital Bath.mp3",
        file_format="mp3",
        title="Digital Bath",
        normalized_title="digital bath",
        year=2000,
        duration_seconds=240,
        file_size=100,
        file_modified_ns=1,
    )
    client.force_login(user)

    response = client.post(reverse("delete-root", args=[root.pk]), {"confirm": "yes"})

    assert response.status_code == 302
    assert Track.objects.filter(pk=second_track.pk).exists()
    assert type(album).objects.filter(pk=album.pk).exists()
    assert type(artist).objects.filter(pk=artist.pk).exists()


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
@override_settings(STORAGES=TEST_STORAGES)
def test_job_history_lists_and_filters_all_jobs(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    JobRun.objects.create(
        job_type="enrich_musicbrainz",
        status=JobRun.Status.FAILED,
        error="temporary TLS failure",
        current_item="/music/current.flac",
        requested_manually=False,
    )
    JobRun.objects.create(
        job_type="generate_playlists",
        status=JobRun.Status.SUCCEEDED,
        summary={"playlists": 4},
        requested_manually=True,
    )
    client.force_login(user)

    response = client.get(reverse("job-history"), {"status": "failed"})
    body = response.content.decode()

    assert response.status_code == 200
    assert all(
        value in body
        for value in (
            "Jobs",
            "enrich_musicbrainz",
            "temporary TLS failure",
            "/music/current.flac",
        )
    )
    assert "<strong>generate_playlists</strong>" not in body
    assert 'href="/jobs/"' in body


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_job_history_reconciles_impossible_running_job(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    job = JobRun.objects.create(
        job_type="scan_library",
        status=JobRun.Status.RUNNING,
        started_at=timezone.now(),
        finished_at=timezone.now(),
    )
    client.force_login(user)

    client.get(reverse("job-history"))

    job.refresh_from_db()
    assert job.status == JobRun.Status.FAILED
    assert "heartbeat" in job.error


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_job_history_can_cancel_running_job(client, django_user_model, monkeypatch):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    job = JobRun.objects.create(
        job_type="enrich_library",
        status=JobRun.Status.RUNNING,
        celery_task_id="task-123",
        started_at=timezone.now(),
        heartbeat_at=timezone.now(),
    )
    revoked = []
    monkeypatch.setattr(
        "enrichment.job_control.current_app.control.revoke",
        lambda task_id, terminate=False: revoked.append((task_id, terminate)),
    )
    client.force_login(user)

    response = client.post(reverse("cancel-job", args=[job.pk]))

    assert response.status_code == 302
    job.refresh_from_db()
    assert job.status == JobRun.Status.CANCELLED
    assert revoked == [("task-123", False)]


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_missing_albums_page_lists_release_and_navigation(client, django_user_model, artist):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    record = SourceRecord.objects.create(
        source=Source.MUSICBRAINZ,
        entity_kind="release_group",
        external_id="missing-album",
        canonical_url="https://musicbrainz.org/release-group/missing-album",
        fetched_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
    )
    MissingAlbum.objects.create(
        artist=artist,
        source=Source.MUSICBRAINZ,
        source_record=record,
        external_id="missing-album",
        title="Saturday Night Wrist",
        normalized_title="saturday night wrist",
        year=2006,
        release_type="Album",
    )
    hidden_record = SourceRecord.objects.create(
        source=Source.MUSICBRAINZ,
        entity_kind="release_group",
        external_id="missing-without-notable-tracks",
        fetched_at=timezone.now(),
    )
    MissingAlbum.objects.create(
        artist=artist,
        source=Source.MUSICBRAINZ,
        source_record=hidden_record,
        external_id="missing-without-notable-tracks",
        title="No Notable Songs Here",
        normalized_title="no notable songs here",
        year=2010,
        release_type="Album",
    )
    track_record = SourceRecord.objects.create(
        source=Source.SPOTIFY,
        entity_kind="track",
        external_id="notable-missing-track",
        fetched_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
    )
    external = ExternalTrack.objects.create(
        source_record=track_record,
        artist=artist,
        artist_name=artist.name,
        title="Hole in the Earth",
        album_title="Saturday Night Wrist",
        rank=1,
        match_decision=Decision.REJECTED,
    )
    NoteworthyEvidence.objects.create(
        artist=artist,
        external_track=external,
        evidence_type=NoteworthyEvidence.EvidenceType.SPOTIFY_TOP,
        confidence=Decimal("0"),
        decision=Decision.REJECTED,
    )
    client.force_login(user)

    response = client.get(reverse("missing-albums"), {"release_type": "Album"})
    body = response.content.decode()

    assert response.status_code == 200
    assert all(
        value in body
        for value in ("Missing", "Deftones", "Saturday Night Wrist", "2006", "Release type")
    )
    assert "No Notable Songs Here" not in body


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
            "track_match_review_threshold": "0.850",
            "track_match_auto_accept_threshold": "0.950",
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


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES, SECRET_KEY="credential-test-secret")
def test_http_user_agent_save_does_not_run_synchronous_reconciliation(
    client, django_user_model, monkeypatch
):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    monkeypatch.setattr(
        "dashboard.views.refresh_noteworthy_decisions_task.delay",
        lambda *args, **kwargs: pytest.fail("unrelated user-agent save queued reconciliation"),
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
            "track_match_review_threshold": "0.850",
            "track_match_auto_accept_threshold": "0.950",
            "http_user_agent": "Kuratorr/1.0 (admin@example.com)",
        },
    )

    assert response.status_code == 302
    assert ServiceSettings.load().http_user_agent == "Kuratorr/1.0 (admin@example.com)"


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_settings_page_manages_playlist_output_directories(client, django_user_model, tmp_path):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    output_path = tmp_path / "playlist-output"
    client.force_login(user)

    response = client.post(
        reverse("settings"),
        {"action": "save_playlist_output", "path": str(output_path), "enabled": "on"},
    )

    assert response.status_code == 302
    assert PlaylistOutputRoot.objects.filter(path=str(output_path), enabled=True).exists()
    body = client.get(reverse("settings")).content.decode()
    assert "Playlist output directory" in body
    assert str(output_path) in body

    replacement = tmp_path / "replacement-output"
    response = client.post(
        reverse("settings"),
        {"action": "save_playlist_output", "path": str(replacement), "enabled": "on"},
    )
    assert response.status_code == 302
    assert PlaylistOutputRoot.objects.count() == 1
    assert PlaylistOutputRoot.load().path == str(replacement)
