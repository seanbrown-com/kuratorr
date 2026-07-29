from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from enrichment.models import Decision, NoteworthyEvidence
from library.models import ServiceSettings
from playlists.models import Playlist, PlaylistOutputRoot
from playlists.services import (
    delete_playlist,
    generate_artist_playlists,
    generate_grouped_playlists,
    generate_radio_playlists,
    materialize_playlist,
    render_copy_script,
    render_m3u,
    render_m3u_zip,
    restore_playlist,
    upsert_playlist,
)


@pytest.fixture
def evidence(track, artist):
    settings = ServiceSettings.load()
    settings.minimum_playlist_tracks = 1
    settings.save(update_fields=["minimum_playlist_tracks", "updated_at"])
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
def test_grouped_playlist_requires_minimum_track_count(evidence):
    settings = ServiceSettings.load()
    settings.minimum_playlist_tracks = 2
    settings.save()
    assert generate_grouped_playlists() == 0
    settings.minimum_playlist_tracks = 1
    settings.save()
    assert generate_grouped_playlists() >= 2


@pytest.mark.django_db
def test_artist_playlist_below_minimum_is_pruned_with_materialized_file(evidence, artist, tmp_path):
    PlaylistOutputRoot.objects.create(path=str(tmp_path), enabled=True)
    generate_artist_playlists()
    playlist = Playlist.objects.get(playlist_type=Playlist.PlaylistType.ARTIST)
    output_path = materialize_playlist(playlist)[0]
    settings = ServiceSettings.load()
    settings.minimum_playlist_tracks = 2
    settings.save(update_fields=["minimum_playlist_tracks", "updated_at"])

    assert generate_artist_playlists() == 0

    assert not Playlist.objects.filter(pk=playlist.pk).exists()
    assert not Path(output_path).exists()


@pytest.mark.django_db
def test_featured_artist_playlists_are_pruned(evidence, track, artist):
    artist_playlist, _ = upsert_playlist(
        f"Best of {artist.name}",
        Playlist.PlaylistType.ARTIST,
        [track],
        artist=artist,
    )
    radio_playlist, _ = upsert_playlist(
        f"{artist.name} Radio",
        Playlist.PlaylistType.ARTIST_RADIO,
        [track],
        artist=artist,
    )
    artist.name = "Deftones feat. Maynard James Keenan"
    artist.normalized_name = "deftones feat maynard james keenan"
    artist.save(update_fields=["name", "normalized_name", "updated_at"])

    assert generate_artist_playlists() == 0
    assert generate_radio_playlists() == 0

    assert not Playlist.objects.filter(pk=artist_playlist.pk).exists()
    assert not Playlist.objects.filter(pk=radio_playlist.pk).exists()


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
    m3u = render_m3u(playlist)
    script = render_copy_script(playlist)
    assert m3u.startswith("#EXTM3U")
    assert "Deftones - Change" in m3u
    assert track.full_path in m3u
    assert "set -euo pipefail" in script
    assert "SOURCE_DIR DESTINATION_DIR" in script
    assert 'source_root="$1"' in script
    assert 'destination_root="$2"' in script
    assert "Best_of_Deftones" in script
    assert track.full_path not in script
    assert track.relative_path in script
    assert "001 - Change.mp3" in script


@pytest.mark.django_db
def test_all_playlists_zip_uses_type_directories(evidence, track):
    generate_artist_playlists()
    content = render_m3u_zip(Playlist.objects.all())
    with ZipFile(BytesIO(content)) as archive:
        assert archive.namelist() == ["best_of_artist/Best_of_Deftones.m3u"]
        m3u = archive.read(archive.namelist()[0]).decode()
    assert track.full_path in m3u


@pytest.mark.django_db
def test_materialized_playlists_use_single_root_and_type_directory(evidence, tmp_path):
    generate_artist_playlists()
    playlist = Playlist.objects.get()
    PlaylistOutputRoot.objects.create(path=str(tmp_path), enabled=True)

    written = materialize_playlist(playlist)

    expected = tmp_path / "best_of_artist" / "Best_of_Deftones.m3u"
    assert written == [str(expected)]
    assert expected.exists()


@pytest.mark.django_db
def test_relative_music_mapping_accounts_for_playlist_type_directory(evidence, track, tmp_path):
    generate_artist_playlists()
    playlist = Playlist.objects.get()
    PlaylistOutputRoot.objects.create(
        path=str(tmp_path),
        music_relative_path="../..",
        enabled=True,
    )

    written = Path(materialize_playlist(playlist)[0])

    assert written == tmp_path / "best_of_artist" / "Best_of_Deftones.m3u"
    assert "../../../Change.mp3" in written.read_text(encoding="utf-8")
    assert track.full_path not in written.read_text(encoding="utf-8")
