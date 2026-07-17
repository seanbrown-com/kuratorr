from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile

import pytest

from enrichment.models import Decision, NoteworthyEvidence
from library.models import ServiceSettings
from playlists.models import Playlist
from playlists.services import (
    delete_playlist,
    generate_artist_playlists,
    generate_grouped_playlists,
    render_copy_script,
    render_m3u,
    render_m3u_zip,
    restore_playlist,
)


@pytest.fixture
def evidence(track, artist):
    return NoteworthyEvidence.objects.create(
        artist=artist,
        track=track,
        evidence_type=NoteworthyEvidence.EvidenceType.MANUAL,
        confidence=Decimal("1"),
        decision=Decision.ACCEPTED,
    )


@pytest.mark.django_db
def test_artist_playlist_generation(evidence, track, artist):
    assert generate_artist_playlists() == 1
    playlist = Playlist.objects.get(playlist_type=Playlist.PlaylistType.ARTIST)
    assert playlist.entries.get().track == track
    assert playlist.name == "Best of Deftones"


@pytest.mark.django_db
def test_grouped_playlist_requires_minimum_duration(evidence):
    settings = ServiceSettings.load()
    settings.minimum_playlist_seconds = 3600
    settings.save()
    assert generate_grouped_playlists() == 0
    settings.minimum_playlist_seconds = 60
    settings.save()
    assert generate_grouped_playlists() >= 2


@pytest.mark.django_db
def test_deleted_playlist_is_not_regenerated_and_can_restore(evidence):
    generate_artist_playlists()
    playlist = Playlist.objects.get()
    delete_playlist(playlist, permanent=True)
    assert generate_artist_playlists() == 0
    playlist.refresh_from_db()
    assert playlist.deleted_at and playlist.never_regenerate
    restore_playlist(playlist)
    playlist.refresh_from_db()
    assert playlist.deleted_at is None and not playlist.never_regenerate


@pytest.mark.django_db
def test_exports_include_ordered_track_metadata_and_safe_path(evidence, track):
    generate_artist_playlists()
    playlist = Playlist.objects.get()
    m3u = render_m3u(playlist, "/Volumes/External Music")
    script = render_copy_script(playlist)
    assert m3u.startswith("#EXTM3U")
    assert "Deftones - Change" in m3u
    assert "/Volumes/External Music/Change.mp3" in m3u
    assert track.full_path not in m3u
    assert "set -euo pipefail" in script
    assert "SOURCE_DIR DESTINATION_DIR" in script
    assert 'source_root="$1"' in script
    assert 'destination_root="$2"' in script
    assert "Best_of_Deftones" in script
    assert track.full_path not in script
    assert track.relative_path in script
    assert "001 - Change.mp3" in script


@pytest.mark.django_db
def test_all_playlists_zip_uses_external_source_directory(evidence):
    generate_artist_playlists()
    content = render_m3u_zip(Playlist.objects.all(), r"D:\Music")
    with ZipFile(BytesIO(content)) as archive:
        assert archive.namelist() == ["Best_of_Deftones.m3u"]
        m3u = archive.read(archive.namelist()[0]).decode()
    assert r"D:\Music\Change.mp3" in m3u
