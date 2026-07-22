from pathlib import Path
from unittest.mock import patch

import pytest

from enrichment.models import JobRun
from library.models import AlbumGenre, ScanIssue, Track
from library.services import (
    import_file,
    normalize_text,
    parse_number,
    parse_year,
    scan_library_root,
)


def test_normalize_text_removes_punctuation_and_version_noise():
    assert normalize_text("Café — Change (Remastered 2020)") == "cafe change"


@pytest.mark.parametrize(("value", "expected"), [("03/12", 3), ("Disc 2", 2), (None, None)])
def test_parse_number(value, expected):
    assert parse_number(value) == expected


def test_parse_year():
    assert parse_year("2000-06-20") == 2000
    assert parse_year("unknown") is None


def test_track_model_does_not_persist_raw_metadata():
    assert "raw_metadata" not in {field.name for field in Track._meta.fields}


@pytest.mark.django_db
def test_import_file_creates_entities_and_genre(root, tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"fake")
    metadata = {
        "title": "My Own Summer",
        "artist": "Deftones",
        "album_artist": "Deftones",
        "album": "Around the Fur",
        "year": 1997,
        "track_number": 1,
        "disc_number": 1,
        "duration_seconds": 215.2,
        "bitrate": 320000,
        "sample_rate": 44100,
        "channels": 2,
        "genres": ["Alternative Metal; Nu Metal"],
    }
    with patch("library.services.read_audio_metadata", return_value=metadata):
        track, created = import_file(root, path)
    assert created
    assert track.album.title == "Around the Fur"
    assert track.artist.name == "Deftones"
    assert list(
        AlbumGenre.objects.filter(album=track.album).values_list("genre__name", flat=True)
    ) == ["Alternative Metal", "Nu Metal"]


@pytest.mark.django_db
def test_scan_marks_missing_tracks_unavailable(root, track):
    summary = scan_library_root(root)
    track.refresh_from_db()
    assert summary["found"] == 0
    assert not track.is_available


@pytest.mark.django_db
def test_scan_records_bad_file(root, tmp_path):
    path = Path(root.path) / "broken.flac"
    path.write_bytes(b"not flac")
    summary = scan_library_root(root)
    assert summary["errors"] == 1
    assert ScanIssue.objects.filter(full_path=str(path), resolved_at=None).exists()


@pytest.mark.django_db
def test_scan_task_reports_progress_without_starting_enrichment(root, monkeypatch):
    from library.tasks import scan_root_task

    def fake_scan(current_root, progress_callback):
        progress_callback(0, 2)
        progress_callback(1, 2)
        progress_callback(2, 2)
        return {"found": 2, "created": 2, "updated_or_unchanged": 0, "errors": 0}

    monkeypatch.setattr("library.tasks.scan_library_root", fake_scan)
    job = JobRun.objects.create(job_type="scan_library", requested_manually=True)

    result = scan_root_task(root.pk, job.pk)

    job.refresh_from_db()
    assert result["found"] == 2
    assert job.status == JobRun.Status.SUCCEEDED
    assert (job.progress_current, job.progress_total) == (2, 2)
    assert not JobRun.objects.exclude(pk=job.pk).exists()
