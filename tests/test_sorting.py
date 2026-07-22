from decimal import Decimal

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from enrichment.models import (
    ArtistRecommendation,
    Decision,
    ExternalTrack,
    MissingAlbum,
    NoteworthyEvidence,
    Source,
    SourceRecord,
)
from library.models import Album, Artist, Track
from playlists.models import Playlist

TEST_STORAGES = {
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
}


def login_admin(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        "admin", password="Very-Long-Test-Passphrase!"
    )
    client.force_login(user)


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_artist_sorting_defaults_to_name_and_supports_counts(
    client, django_user_model, root, artist, album, track
):
    second_artist = Artist.objects.create(
        name="Alice in Chains", sort_name="Alice in Chains", normalized_name="alice in chains"
    )
    for index in range(2):
        second_album = Album.objects.create(
            artist=second_artist,
            title=f"Album {index}",
            normalized_title=f"album {index}",
            year=1990 + index,
        )
        Track.objects.create(
            library_root=root,
            artist=second_artist,
            album=second_album,
            full_path=f"{root.path}/alice-{index}.mp3",
            relative_path=f"alice-{index}.mp3",
            file_format="mp3",
            title=f"Track {index}",
            normalized_title=f"track {index}",
            duration_seconds=180,
        )
    login_admin(client, django_user_model)

    response = client.get(reverse("artist-list"))
    assert [item.name for item in response.context["page"].object_list] == [
        "Alice in Chains",
        "Deftones",
    ]

    response = client.get(reverse("artist-list"), {"sort": "tracks", "direction": "desc"})
    assert response.context["page"].object_list[0].name == "Alice in Chains"
    assert all(
        value in response.content for value in (b"sort=artist", b"sort=albums", b"sort=tracks")
    )


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_discover_sorts_by_rank_or_recommended_artist(client, django_user_model):
    ArtistRecommendation.objects.create(
        name="Zulu", normalized_name="zulu", rank=1, linked_artist_count=1
    )
    ArtistRecommendation.objects.create(
        name="Alpha", normalized_name="alpha", rank=2, linked_artist_count=1
    )
    login_admin(client, django_user_model)

    response = client.get(
        reverse("artist-recommendations"),
        {"sort": "recommended", "direction": "asc"},
    )

    assert [item.name for item in response.context["page"].object_list] == ["Alpha", "Zulu"]
    assert b"sort=rank" in response.content and b"sort=recommended" in response.content


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_missing_albums_supports_only_requested_sort_columns(
    client, django_user_model, artist, monkeypatch
):
    record = SourceRecord.objects.create(
        source=Source.MUSICBRAINZ,
        entity_kind="release_group",
        external_id="missing-sort-source",
        fetched_at=timezone.now(),
    )
    MissingAlbum.objects.create(
        artist=artist,
        source=Source.MUSICBRAINZ,
        source_record=record,
        external_id="z-album",
        title="Zulu Album",
        normalized_title="zulu album",
        year=2001,
        release_type="Album",
    )
    MissingAlbum.objects.create(
        artist=artist,
        source=Source.MUSICBRAINZ,
        source_record=record,
        external_id="a-album",
        title="Alpha Album",
        normalized_title="alpha album",
        year=1999,
        release_type="EP",
    )
    monkeypatch.setattr(
        "enrichment.views.missing_albums_with_notable_tracks", lambda albums: list(albums)
    )
    login_admin(client, django_user_model)

    response = client.get(reverse("missing-albums"), {"sort": "album"})

    assert [item.title for item in response.context["page"].object_list] == [
        "Alpha Album",
        "Zulu Album",
    ]
    body = response.content
    assert all(
        value in body
        for value in (b"sort=artist", b"sort=album", b"sort=year", b"sort=release_type")
    )
    assert b"sort=source" not in body


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_review_sorts_by_first_five_columns(client, django_user_model, artist, track):
    for index, confidence in enumerate((Decimal("0.86"), Decimal("0.94"))):
        record = SourceRecord.objects.create(
            source=Source.WIKIPEDIA,
            entity_kind="track",
            external_id=f"review-sort-{index}",
            fetched_at=timezone.now(),
        )
        external = ExternalTrack.objects.create(
            source_record=record,
            artist=artist,
            matched_track=track,
            artist_name=artist.name,
            title=f"Candidate {index}",
            match_confidence=confidence,
            match_decision=Decision.PENDING,
        )
        NoteworthyEvidence.objects.create(
            artist=artist,
            track=track,
            external_track=external,
            evidence_type=NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
            confidence=confidence,
            decision=Decision.PENDING,
        )
    login_admin(client, django_user_model)

    response = client.get(reverse("review-queue"), {"sort": "confidence", "direction": "desc"})

    assert [item.confidence for item in response.context["page"].object_list] == [
        Decimal("0.940"),
        Decimal("0.860"),
    ]
    body = response.content
    assert all(
        value in body
        for value in (
            b"sort=source",
            b"sort=artist",
            b"sort=external_title",
            b"sort=local_match",
            b"sort=confidence",
        )
    )
    assert b"sort=evidence" not in body


@pytest.mark.django_db
@override_settings(STORAGES=TEST_STORAGES)
def test_playlists_sort_by_first_four_columns(client, django_user_model):
    Playlist.objects.create(
        name="Short",
        playlist_type=Playlist.PlaylistType.YEAR,
        definition_key="year:short",
        track_count=2,
        duration_seconds=300,
    )
    Playlist.objects.create(
        name="Long",
        playlist_type=Playlist.PlaylistType.ARTIST,
        definition_key="artist:long",
        track_count=10,
        duration_seconds=2400,
    )
    login_admin(client, django_user_model)

    response = client.get(reverse("playlist-list"), {"sort": "duration", "direction": "desc"})

    assert [item.name for item in response.context["playlists"]] == ["Long", "Short"]
    body = response.content
    assert all(
        value in body for value in (b"sort=name", b"sort=type", b"sort=tracks", b"sort=duration")
    )
    assert b"sort=generated" not in body
