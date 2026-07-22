import re
import shlex
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.db import transaction
from django.utils import timezone

from enrichment.models import Decision, NoteworthyEvidence, RelatedArtistEvidence
from library.models import Artist, ServiceSettings
from playlists.models import Playlist, PlaylistOutputRoot, PlaylistTrack

PLAYLIST_DIRECTORIES = {
    Playlist.PlaylistType.ARTIST: "best of artist",
    Playlist.PlaylistType.YEAR: "best of year",
    Playlist.PlaylistType.DECADE: "best of decades",
    Playlist.PlaylistType.GENRE: "best of genres",
    Playlist.PlaylistType.GENRE_YEAR: "genres by year",
    Playlist.PlaylistType.GENRE_DECADE: "genres by decade",
    Playlist.PlaylistType.ARTIST_RADIO: "artist radio",
}


def _safe_filename(name):
    value = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip().replace(" ", "_")
    return value[:180] or "playlist"


def noteworthy_tracks(artist=None):
    evidence = NoteworthyEvidence.objects.filter(
        decision=Decision.ACCEPTED, track__is_available=True
    ).exclude(track=None)
    if artist:
        evidence = evidence.filter(artist=artist)
    best = {}
    for item in evidence.select_related("track", "artist", "external_track"):
        rank = (
            item.external_track.rank if item.external_track and item.external_track.rank else 9999
        )
        score = (float(item.confidence), -rank)
        if item.track_id not in best or score > best[item.track_id][0]:
            best[item.track_id] = (score, item.track)
    return [value[1] for value in sorted(best.values(), key=lambda x: x[0], reverse=True)]


def _definition_key(playlist_type, **values):
    parts = [playlist_type] + [f"{key}={values[key]}" for key in sorted(values)]
    return "|".join(parts)


@transaction.atomic
def upsert_playlist(name, playlist_type, tracks, **dimensions):
    key_values = {
        key: value.pk if hasattr(value, "pk") else value
        for key, value in dimensions.items()
        if value is not None
    }
    key = _definition_key(playlist_type, **key_values)
    existing = Playlist.objects.filter(definition_key=key).first()
    if existing and existing.deleted_at:
        return existing, False
    playlist, _ = Playlist.objects.update_or_create(
        definition_key=key,
        defaults={
            "name": name,
            "playlist_type": playlist_type,
            **dimensions,
            "deleted_at": None,
            "never_regenerate": False,
        },
    )
    PlaylistTrack.objects.filter(playlist=playlist).delete()
    duration = 0
    seen = set()
    position = 0
    for track in tracks:
        if track.pk in seen:
            continue
        seen.add(track.pk)
        position += 1
        duration += int(track.duration_seconds or 0)
        PlaylistTrack.objects.create(playlist=playlist, track=track, position=position)
    playlist.track_count = position
    playlist.duration_seconds = duration
    playlist.generated_at = timezone.now()
    playlist.save()
    return playlist, True


def _year_for(track):
    return track.year or track.album.year


def generate_artist_playlists():
    count = 0
    for artist in Artist.objects.all():
        tracks = noteworthy_tracks(artist)
        if tracks:
            _, written = upsert_playlist(
                f"Best of {artist.name}", Playlist.PlaylistType.ARTIST, tracks, artist=artist
            )
            count += int(written)
    return count


def generate_grouped_playlists():
    minimum = ServiceSettings.load().minimum_playlist_seconds
    tracks = noteworthy_tracks()
    years = defaultdict(list)
    decades = defaultdict(list)
    genres = defaultdict(list)
    genre_years = defaultdict(list)
    genre_decades = defaultdict(list)
    for track in tracks:
        year = _year_for(track)
        if year:
            years[year].append(track)
            decades[(year // 10) * 10].append(track)
        for assignment in track.album.genre_assignments.select_related("genre"):
            genres[assignment.genre].append(track)
            if year:
                genre_years[(assignment.genre, year)].append(track)
                genre_decades[(assignment.genre, (year // 10) * 10)].append(track)
    created = 0

    def enough(items):
        return (
            sum(int(x.duration_seconds or 0) for x in {x.pk: x for x in items}.values()) >= minimum
        )

    for year, items in years.items():
        if enough(items):
            created += int(
                upsert_playlist(f"Best of {year}", Playlist.PlaylistType.YEAR, items, year=year)[1]
            )
    for decade, items in decades.items():
        if enough(items):
            created += int(
                upsert_playlist(
                    f"Best of the {decade}s", Playlist.PlaylistType.DECADE, items, decade=decade
                )[1]
            )
    for genre, items in genres.items():
        if enough(items):
            created += int(
                upsert_playlist(
                    f"Best of {genre.name}", Playlist.PlaylistType.GENRE, items, genre=genre
                )[1]
            )
    for (genre, year), items in genre_years.items():
        if enough(items):
            created += int(
                upsert_playlist(
                    f"Best of {year} {genre.name}",
                    Playlist.PlaylistType.GENRE_YEAR,
                    items,
                    genre=genre,
                    year=year,
                )[1]
            )
    for (genre, decade), items in genre_decades.items():
        if enough(items):
            created += int(
                upsert_playlist(
                    f"{decade}s {genre.name} Hits",
                    Playlist.PlaylistType.GENRE_DECADE,
                    items,
                    genre=genre,
                    decade=decade,
                )[1]
            )
    return created


def generate_radio_playlists():
    minimum = ServiceSettings.load().minimum_playlist_seconds
    created = 0
    for artist in Artist.objects.all():
        primary = noteworthy_tracks(artist)
        related_ids = RelatedArtistEvidence.objects.filter(
            artist=artist,
            decision=Decision.ACCEPTED,
            related_artist__isnull=False,
        ).values_list("related_artist_id", flat=True)
        related = []
        for related_artist in Artist.objects.filter(pk__in=related_ids):
            related.extend(noteworthy_tracks(related_artist)[:10])
        combined = []
        while primary or related:
            if primary:
                combined.append(primary.pop(0))
            if related:
                combined.append(related.pop(0))
        if (
            sum(int(x.duration_seconds or 0) for x in {x.pk: x for x in combined}.values())
            >= minimum
        ):
            _, written = upsert_playlist(
                f"{artist.name} Radio", Playlist.PlaylistType.ARTIST_RADIO, combined, artist=artist
            )
            created += int(written)
    return created


def playlist_relative_path(playlist):
    directory = PLAYLIST_DIRECTORIES[playlist.playlist_type]
    return Path(directory) / f"{_safe_filename(playlist.name)}.m3u"


def render_m3u(playlist):
    lines = ["#EXTM3U"]
    for entry in playlist.entries.select_related("track", "track__artist"):
        track = entry.track
        display = f"{track.artist.name} - {track.title}".replace("\r", " ").replace("\n", " ")
        lines.append(f"#EXTINF:{int(track.duration_seconds or 0)},{display}")
        lines.append(track.full_path)
    return "\n".join(lines) + "\n"


def render_m3u_zip(playlists):
    archive = BytesIO()
    used_names = set()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        for playlist in playlists:
            base_name = _safe_filename(playlist.name)
            filename = str(playlist_relative_path(playlist)).replace("\\", "/")
            if filename.casefold() in used_names:
                directory = PLAYLIST_DIRECTORIES[playlist.playlist_type]
                filename = f"{directory}/{base_name}-{playlist.pk}.m3u"
            used_names.add(filename.casefold())
            zip_file.writestr(filename, render_m3u(playlist))
    return archive.getvalue()


def materialize_playlist(playlist):
    output_root = PlaylistOutputRoot.load()
    if not output_root or not output_root.enabled:
        playlist.output_path = ""
        playlist.save(update_fields=["output_path", "updated_at"])
        return []
    destination = Path(output_root.path) / playlist_relative_path(playlist)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".m3u.tmp")
    temporary.write_text(render_m3u(playlist), encoding="utf-8")
    temporary.replace(destination)
    playlist.output_path = str(destination)
    playlist.save(update_fields=["output_path", "updated_at"])
    return [str(destination)]


def materialize_all():
    return sum(
        len(materialize_playlist(playlist)) for playlist in Playlist.objects.filter(deleted_at=None)
    )


def render_copy_script(playlist):
    name = _safe_filename(playlist.name)
    entries = list(playlist.entries.select_related("track"))
    sources = "\n".join(f"  {shlex.quote(entry.track.relative_path)}" for entry in entries)
    names = "\n".join(
        f"  {shlex.quote(f'{entry.position:03d} - {Path(entry.track.full_path).name}')}"
        for entry in entries
    )
    return f"""#!/bin/bash
set -euo pipefail
if [[ $# -ne 2 ]]; then
  printf 'Usage: %s SOURCE_DIR DESTINATION_DIR\n' "$0" >&2
  exit 64
fi
source_root="$1"
destination_root="$2"
destination="${{destination_root%/}}/{name}"
mkdir -p "$destination"
sources=(
{sources}
)
names=(
{names}
)
for index in "${{!sources[@]}}"; do
  source_path="${{source_root%/}}/${{sources[$index]}}"
  cp --preserve=timestamps -- "$source_path" "$destination/${{names[$index]}}"
done
printf 'Copied %s tracks to %s\\n' "${{#sources[@]}}" "$destination"
"""


def delete_playlist(playlist, permanent=False):
    playlist.deleted_at = timezone.now()
    playlist.never_regenerate = permanent
    playlist.save(update_fields=["deleted_at", "never_regenerate", "updated_at"])


def restore_playlist(playlist):
    playlist.deleted_at = None
    playlist.never_regenerate = False
    playlist.save(update_fields=["deleted_at", "never_regenerate", "updated_at"])
