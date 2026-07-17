import re
import unicodedata
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from enrichment.models import AlbumGenreEvidence, Decision, Source
from library.models import Album, AlbumGenre, Artist, Genre, ScanIssue, Track

SUPPORTED_EXTENSIONS = {".mp3": Track.AudioFormat.MP3, ".flac": Track.AudioFormat.FLAC}


def normalize_text(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = re.sub(r"\([^)]*(remaster|version|edit|mix)[^)]*\)", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def parse_number(value):
    if value in (None, ""):
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def parse_year(value):
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return int(match.group()) if match else None


def _first(mapping, *keys, default=""):
    for key in keys:
        value = mapping.get(key)
        if value:
            if isinstance(value, list):
                return str(value[0])
            return str(value)
    return default


def _mp3_metadata(path):
    audio = MP3(path)
    tags = audio.tags or {}
    values = {
        "title": _first(tags, "TIT2"),
        "artist": _first(tags, "TPE1"),
        "album_artist": _first(tags, "TPE2"),
        "album": _first(tags, "TALB", default="Unknown Album"),
        "date": _first(tags, "TDRC", "TDOR", "TYER"),
        "track": _first(tags, "TRCK"),
        "disc": _first(tags, "TPOS"),
        "genres": [str(x) for x in tags.getall("TCON")],
    }
    raw = {
        key: [str(x) for x in value] if isinstance(value, list) else str(value)
        for key, value in tags.items()
    }
    return values, raw, audio.info


def _flac_metadata(path):
    audio = FLAC(path)
    tags = audio.tags or {}
    values = {
        "title": _first(tags, "title"),
        "artist": _first(tags, "artist"),
        "album_artist": _first(tags, "albumartist", "album artist"),
        "album": _first(tags, "album", default="Unknown Album"),
        "date": _first(tags, "date", "originaldate"),
        "track": _first(tags, "tracknumber"),
        "disc": _first(tags, "discnumber"),
        "genres": list(tags.get("genre", [])),
    }
    raw = {key: [str(x) for x in value] for key, value in tags.items()}
    return values, raw, audio.info


def read_audio_metadata(path):
    parser = _mp3_metadata if path.suffix.lower() == ".mp3" else _flac_metadata
    values, raw, info = parser(path)
    if not values["title"] or not values["artist"]:
        raise ValueError("Required title or artist tag is missing")
    values["album_artist"] = values["album_artist"] or values["artist"]
    return {
        **values,
        "year": parse_year(values["date"]),
        "track_number": parse_number(values["track"]),
        "disc_number": parse_number(values["disc"]),
        "duration_seconds": round(float(getattr(info, "length", 0)), 3),
        "bitrate": getattr(info, "bitrate", None),
        "sample_rate": getattr(info, "sample_rate", None),
        "channels": getattr(info, "channels", None),
        "raw": raw,
    }


def _artist(name):
    normalized = normalize_text(name)
    artist, _ = Artist.objects.get_or_create(
        normalized_name=normalized, defaults={"name": name, "sort_name": name}
    )
    return artist


def _genres(album, values):
    names = []
    for raw in values:
        names.extend(re.split(r"[;/]", str(raw)))
    for raw_name in filter(None, (x.strip() for x in names)):
        normalized = normalize_text(raw_name)
        if not normalized:
            continue
        genre, _ = Genre.objects.get_or_create(
            normalized_name=normalized, defaults={"name": raw_name}
        )
        AlbumGenreEvidence.objects.get_or_create(
            album=album,
            genre=genre,
            source=Source.LOCAL,
            defaults={"confidence": 1, "decision": Decision.ACCEPTED},
        )
    accepted = album.genre_evidence.filter(decision=Decision.ACCEPTED).order_by(
        "-confidence", "genre__name"
    )[:3]
    AlbumGenre.objects.filter(album=album, is_manual=False).delete()
    for rank, evidence in enumerate(accepted, 1):
        AlbumGenre.objects.update_or_create(
            album=album,
            genre=evidence.genre,
            defaults={"rank": rank, "confidence": evidence.confidence},
        )


@transaction.atomic
def import_file(root, path):
    stat = path.stat()
    existing = Track.objects.filter(full_path=str(path)).first()
    if (
        existing
        and existing.file_modified_ns == stat.st_mtime_ns
        and existing.file_size == stat.st_size
    ):
        if not existing.is_available:
            existing.is_available = True
            existing.save(update_fields=["is_available", "updated_at"])
        return existing, False
    metadata = read_audio_metadata(path)
    artist = _artist(metadata["artist"])
    album_artist = _artist(metadata["album_artist"])
    album, _ = Album.objects.get_or_create(
        artist=album_artist,
        normalized_title=normalize_text(metadata["album"]),
        year=metadata["year"],
        defaults={"title": metadata["album"]},
    )
    track, created = Track.objects.update_or_create(
        full_path=str(path),
        defaults={
            "library_root": root,
            "relative_path": str(path.relative_to(Path(root.path).resolve())),
            "file_format": SUPPORTED_EXTENSIONS[path.suffix.lower()],
            "artist": artist,
            "album": album,
            "title": metadata["title"],
            "normalized_title": normalize_text(metadata["title"]),
            "year": metadata["year"],
            "track_number": metadata["track_number"],
            "disc_number": metadata["disc_number"],
            "duration_seconds": metadata["duration_seconds"],
            "bitrate": metadata["bitrate"],
            "sample_rate": metadata["sample_rate"],
            "channels": metadata["channels"],
            "file_size": stat.st_size,
            "file_modified_ns": stat.st_mtime_ns,
            "raw_metadata": metadata["raw"],
            "is_available": True,
            "scan_error": "",
        },
    )
    _genres(album, metadata["genres"])
    ScanIssue.objects.filter(library_root=root, full_path=str(path)).update(
        resolved_at=timezone.now()
    )
    return track, created


def scan_library_root(root):
    base = Path(root.path).resolve()
    if not base.is_dir():
        raise ValueError(f"Library root does not exist or is not a directory: {base}")
    seen = []
    summary = {"found": 0, "created": 0, "updated_or_unchanged": 0, "errors": 0}
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        summary["found"] += 1
        seen.append(str(path))
        try:
            _, created = import_file(root, path)
            summary["created" if created else "updated_or_unchanged"] += 1
        except Exception as exc:
            summary["errors"] += 1
            ScanIssue.objects.update_or_create(
                library_root=root,
                full_path=str(path),
                defaults={"error": str(exc), "resolved_at": None},
            )
    Track.objects.filter(library_root=root).exclude(full_path__in=seen).update(is_available=False)
    root.last_scanned_at = timezone.now()
    root.save(update_fields=["last_scanned_at", "updated_at"])
    return summary
