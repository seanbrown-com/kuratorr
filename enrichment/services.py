import re
import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from urllib.parse import unquote

from bs4 import BeautifulSoup
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rapidfuzz import fuzz, process

from enrichment.clients import (
    LastFmClient,
    MusicBrainzClient,
    SpotifyClient,
    WikipediaClient,
    YouTubeClient,
    wikipedia_url,
)
from enrichment.models import (
    AlbumGenreEvidence,
    ArtistRecommendation,
    Decision,
    ExternalIdentifier,
    ExternalTrack,
    MissingAlbum,
    NoteworthyDecisionStage,
    NoteworthyEvidence,
    RelatedArtistEvidence,
    Source,
    SourceRecord,
)
from library.models import Album, Artist, Genre, ServiceSettings
from library.services import has_feature_credit, normalize_text, primary_artist_name


def _score(left, right):
    return Decimal(str(round(fuzz.WRatio(normalize_text(left), normalize_text(right)) / 100, 3)))


def _title_key(value):
    """Normalize a song/album title while ignoring common edition suffixes."""
    value = primary_artist_name(value)
    value = re.sub(
        r"\s*[\[(][^\])]*(?:remaster(?:ed)?|version|edit|mix|mono|stereo|live)[^\])]*[\])]\s*$",
        "",
        value or "",
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\s*[-–—:]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|radio edit|single edit|album version|"
        r"original mix|mono|stereo|live)(?:\s+version)?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return normalize_text(value).replace(" and ", " ")


def _title_score(left, right):
    left_key, right_key = _title_key(left), _title_key(right)
    if not left_key or not right_key:
        return Decimal("0")
    if left_key == right_key:
        return Decimal("1")
    return Decimal(str(round(fuzz.ratio(left_key, right_key) / 100, 3)))


def _clean_wikipedia_title(value):
    """Remove rendered citation markers and unmatched display quotes from a title."""
    value = re.sub(
        r"(?:\s*\[\s*(?:\d+|[a-z])\s*\])+\s*$",
        "",
        value or "",
        flags=re.IGNORECASE,
    )
    return value.strip().strip('"“”').strip()


def _wikipedia_node_text(node):
    clone = BeautifulSoup(str(node), "html.parser")
    for reference in clone.select("sup.reference, .mw-ref, .reference"):
        reference.decompose()
    return clone.get_text(" ", strip=True)


def _wikipedia_title_anchor(node):
    return next(
        (
            anchor
            for anchor in node.find_all("a")
            if "cite_note" not in anchor.get("href", "")
            and not anchor.find_parent("sup", class_="reference")
        ),
        None,
    )


def _record(source, kind, external_id, payload, url=""):
    return SourceRecord.objects.update_or_create(
        source=source,
        entity_kind=kind,
        external_id=str(external_id),
        defaults={"payload": payload, "canonical_url": url, "fetched_at": timezone.now()},
    )[0]


def _best_artist_candidate(name, candidates, name_key="name"):
    scored = sorted(
        ((_score(name, c.get(name_key, "")), c) for c in candidates),
        key=lambda x: x[0],
        reverse=True,
    )
    return scored[0] if scored else (Decimal("0"), None)


def _best_album_candidate(artist, album, candidates):
    """Prefer an actual album page and require artist context for fuzzy titles."""
    scored = []
    for candidate in candidates:
        title = candidate.get("title", "")
        base_title = re.sub(r"\s*\(album\)\s*$", "", title, flags=re.IGNORECASE)
        title_score = _title_score(album.title, base_title)
        snippet = BeautifulSoup(candidate.get("snippet", ""), "html.parser").get_text(" ")
        has_artist_context = normalize_text(artist.name) in normalize_text(f"{title} {snippet}")
        exact_album_title = normalize_text(base_title) == album.normalized_title
        self_titled = album.normalized_title == artist.normalized_name
        if exact_album_title and (has_artist_context or self_titled):
            confidence = Decimal("1")
        elif has_artist_context:
            confidence = title_score
        else:
            confidence = title_score * Decimal("0.5")
        scored.append((confidence, candidate))
    return max(scored, key=lambda item: item[0]) if scored else (Decimal("0"), None)


def _match_local_track(
    artist,
    title,
    *,
    year=None,
    album_title="",
    settings=None,
    tracks=None,
    track_index=None,
):
    settings = settings or ServiceSettings.load()
    tracks = tracks if tracks is not None else artist.tracks.filter(is_available=True)
    if hasattr(tracks, "select_related"):
        tracks = list(tracks.select_related("album"))
    else:
        tracks = list(tracks)
    if track_index is None:
        track_index = defaultdict(list)
        for track in tracks:
            key = _title_key(track.title)
            if key:
                track_index[key].append(track)
    query_key = _title_key(title)
    if not query_key or not track_index:
        return None, Decimal("0"), Decision.REJECTED
    match = process.extractOne(query_key, track_index.keys(), scorer=fuzz.ratio)
    if not match:
        return None, Decimal("0"), Decision.REJECTED
    matched_key, score, _ = match
    confidence = Decimal(str(round(score / 100, 3)))
    track = max(
        track_index[matched_key],
        key=lambda candidate: (
            _title_score(album_title, candidate.album.title) if album_title else Decimal("0"),
            -abs((candidate.year or candidate.album.year) - year)
            if year and (candidate.year or candidate.album.year)
            else -9999,
        ),
    )
    if confidence >= settings.track_match_auto_accept_threshold:
        return track, confidence, Decision.ACCEPTED
    if confidence >= settings.track_match_review_threshold:
        return track, confidence, Decision.PENDING
    return None, confidence, Decision.REJECTED


def _external_track(source, record, artist, title, evidence_type, **data):
    matched, confidence, decision = _match_local_track(
        artist,
        title,
        year=data.get("year"),
        album_title=data.get("album_title", ""),
    )
    source_confidence = Decimal(str(data.get("source_confidence", 1)))
    evidence_confidence = min(confidence, source_confidence)
    evidence_decision = (
        Decision.ACCEPTED
        if data.get("auto_qualifies", True)
        and decision == Decision.ACCEPTED
        and source_confidence >= Decimal("0.85")
        else Decision.PENDING
    )
    if decision == Decision.REJECTED:
        evidence_decision = Decision.REJECTED
    if decision == Decision.ACCEPTED and not data.get("auto_qualifies", True):
        evidence_decision = Decision.REJECTED
    external, _ = ExternalTrack.objects.update_or_create(
        source_record=record,
        defaults={
            "artist": artist,
            "matched_track": matched,
            "artist_name": data.get("artist_name", artist.name),
            "title": title,
            "album_title": data.get("album_title", ""),
            "year": data.get("year"),
            "duration_seconds": data.get("duration_seconds"),
            "rank": data.get("rank"),
            "playcount": data.get("playcount"),
            "popularity": data.get("popularity"),
            "match_confidence": confidence,
            "match_decision": evidence_decision,
        },
    )
    evidence, created = NoteworthyEvidence.objects.get_or_create(
        external_track=external,
        evidence_type=evidence_type,
        defaults={
            "artist": artist,
            "track": matched,
            "confidence": evidence_confidence,
            "decision": evidence_decision,
            "notes": data.get("notes", ""),
        },
    )
    if not created:
        if evidence.decision_is_manual:
            external.matched_track = evidence.track
            external.match_decision = evidence.decision
            external.save(update_fields=["matched_track", "match_decision", "updated_at"])
        else:
            evidence.artist = artist
            evidence.track = matched
            evidence.confidence = evidence_confidence
            evidence.decision = evidence_decision
            evidence.notes = data.get("notes", "")
            evidence.save(
                update_fields=[
                    "artist",
                    "track",
                    "confidence",
                    "decision",
                    "notes",
                    "updated_at",
                ]
            )
    return external


def _store_related_artist_evidence(
    *,
    artist,
    raw_name,
    relationship_type,
    source,
    source_record,
    confidence,
):
    """Store a relationship against the lead artist, never a featured credit."""
    name = primary_artist_name(raw_name)
    normalized = normalize_text(name)
    if not normalized or normalized == artist.normalized_name:
        return False
    related = Artist.objects.filter(normalized_name=normalized).first()
    RelatedArtistEvidence.objects.update_or_create(
        artist=artist,
        related_artist_name=name,
        relationship_type=relationship_type,
        source=source,
        defaults={
            "related_artist": related,
            "source_record": source_record,
            "confidence": confidence,
            "decision": Decision.ACCEPTED if related else Decision.PENDING,
        },
    )
    return True


def missing_albums_with_notable_tracks(albums):
    """Return missing releases that contain at least one source-qualified notable track."""
    albums = list(albums)
    if not albums:
        return []
    settings = ServiceSettings.load()
    artist_ids = {album.artist_id for album in albums}
    evidence_items = NoteworthyEvidence.objects.filter(
        artist_id__in=artist_ids,
        external_track__isnull=False,
    ).select_related("external_track")
    candidates = defaultdict(list)
    for evidence in evidence_items:
        external = evidence.external_track
        if not external.album_title:
            continue
        qualifies = evidence.evidence_type in {
            NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
            NoteworthyEvidence.EvidenceType.WIKIPEDIA_VIDEO,
            NoteworthyEvidence.EvidenceType.YOUTUBE_OFFICIAL,
        }
        if evidence.evidence_type == NoteworthyEvidence.EvidenceType.SPOTIFY_TOP:
            qualifies = bool(
                external.rank and external.rank <= settings.spotify_noteworthy_max_rank
            )
        elif evidence.evidence_type == NoteworthyEvidence.EvidenceType.LASTFM_TOP:
            qualifies = bool(
                external.rank
                and external.rank <= settings.lastfm_noteworthy_max_rank
                and external.playcount
                and external.playcount >= settings.lastfm_min_playcount
            )
        if qualifies:
            candidates[evidence.artist_id].append(external)

    visible = []
    for album in albums:
        matches = {
            external.pk
            for external in candidates.get(album.artist_id, [])
            if _title_score(album.title, external.album_title) >= Decimal("0.82")
        }
        if matches:
            album.notable_track_count = len(matches)
            visible.append(album)
    return visible


@transaction.atomic
def enrich_spotify(artist):
    settings = ServiceSettings.load()
    client = SpotifyClient()
    confidence, candidate = _best_artist_candidate(artist.name, client.find_artist(artist.name))
    if not candidate or confidence < Decimal("0.75"):
        return {"tracks": 0, "warning": "No confident Spotify artist match"}
    artist_record = _record(
        Source.SPOTIFY,
        "artist",
        candidate["id"],
        candidate,
        candidate.get("external_urls", {}).get("spotify", ""),
    )
    ExternalIdentifier.objects.update_or_create(
        source=Source.SPOTIFY,
        entity_kind="artist",
        external_id=candidate["id"],
        defaults={
            "artist": artist,
            "source_record": artist_record,
            "confidence": confidence,
            "decision": Decision.ACCEPTED if confidence >= Decimal("0.9") else Decision.PENDING,
        },
    )
    tracks = client.top_tracks(candidate["id"], settings.spotify_market)[
        : settings.spotify_max_tracks
    ]
    NoteworthyEvidence.objects.filter(
        artist=artist,
        evidence_type=NoteworthyEvidence.EvidenceType.SPOTIFY_TOP,
        decision_is_manual=False,
    ).delete()
    for rank, item in enumerate(tracks, 1):
        record = _record(
            Source.SPOTIFY,
            "track",
            item["id"],
            item,
            item.get("external_urls", {}).get("spotify", ""),
        )
        _external_track(
            Source.SPOTIFY,
            record,
            artist,
            item["name"],
            NoteworthyEvidence.EvidenceType.SPOTIFY_TOP,
            artist_name=item.get("artists", [{}])[0].get("name", artist.name),
            album_title=item.get("album", {}).get("name", ""),
            rank=rank,
            popularity=item.get("popularity"),
            duration_seconds=(item.get("duration_ms") or 0) / 1000,
            source_confidence=confidence,
            auto_qualifies=rank <= settings.spotify_noteworthy_max_rank,
            notes=f"Spotify artist top-track rank {rank}; automatic cutoff is {settings.spotify_noteworthy_max_rank}.",
        )
    return {"tracks": len(tracks)}


@transaction.atomic
def enrich_lastfm(artist):
    settings = ServiceSettings.load()
    client = LastFmClient()
    tracks = client.artist_top_tracks(artist.name, settings.lastfm_max_tracks)
    NoteworthyEvidence.objects.filter(
        artist=artist,
        evidence_type=NoteworthyEvidence.EvidenceType.LASTFM_TOP,
        decision_is_manual=False,
    ).delete()
    kept = 0
    for rank, item in enumerate(tracks, 1):
        playcount = int(item.get("playcount") or 0)
        if playcount < settings.lastfm_min_playcount:
            continue
        external_id = (
            item.get("mbid") or f"{normalize_text(artist.name)}:{normalize_text(item['name'])}"
        )
        record = _record(Source.LASTFM, "track", external_id, item, item.get("url", ""))
        _external_track(
            Source.LASTFM,
            record,
            artist,
            item["name"],
            NoteworthyEvidence.EvidenceType.LASTFM_TOP,
            rank=rank,
            playcount=playcount,
            auto_qualifies=rank <= settings.lastfm_noteworthy_max_rank,
            notes=f"Last.fm artist top-track rank {rank}; automatic cutoff is {settings.lastfm_noteworthy_max_rank}.",
        )
        kept += 1
    for item in client.similar_artists(artist.name):
        raw_name = item.get("name", "").strip()
        if not raw_name:
            continue
        record = _record(
            Source.LASTFM,
            "related_artist",
            f"{normalize_text(artist.name)}:{normalize_text(raw_name)}",
            item,
            item.get("url", ""),
        )
        _store_related_artist_evidence(
            artist=artist,
            relationship_type=RelatedArtistEvidence.RelationshipType.SIMILAR,
            source=Source.LASTFM,
            source_record=record,
            raw_name=raw_name,
            confidence=Decimal(str(item.get("match") or 0.5)),
        )
    return {"tracks": kept}


def _year(value):
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return int(match.group()) if match else None


@transaction.atomic
def enrich_musicbrainz(artist):
    client = MusicBrainzClient()
    confidence, candidate = _best_artist_candidate(artist.name, client.find_artist(artist.name))
    if not candidate or confidence < Decimal("0.75"):
        return {"albums": 0, "warning": "No confident MusicBrainz artist match"}
    mbid = candidate["id"]
    record = _record(
        Source.MUSICBRAINZ, "artist", mbid, candidate, f"https://musicbrainz.org/artist/{mbid}"
    )
    ExternalIdentifier.objects.update_or_create(
        source=Source.MUSICBRAINZ,
        entity_kind="artist",
        external_id=mbid,
        defaults={
            "artist": artist,
            "source_record": record,
            "confidence": confidence,
            "decision": Decision.ACCEPTED if confidence >= Decimal("0.9") else Decision.PENDING,
        },
    )
    matched_albums = 0
    single_count = 0
    NoteworthyEvidence.objects.filter(
        artist=artist,
        evidence_type=NoteworthyEvidence.EvidenceType.MUSICBRAINZ_SINGLE,
        decision_is_manual=False,
    ).delete()
    MissingAlbum.objects.filter(artist=artist, source=Source.MUSICBRAINZ).delete()
    for release in client.release_groups(mbid):
        release_title = release.get("title", "").strip()
        if not release_title:
            continue
        release_record = _record(
            Source.MUSICBRAINZ,
            "release_group",
            release["id"],
            release,
            f"https://musicbrainz.org/release-group/{release['id']}",
        )
        if release.get("primary-type") == "Single":
            _external_track(
                Source.MUSICBRAINZ,
                release_record,
                artist,
                release_title,
                NoteworthyEvidence.EvidenceType.MUSICBRAINZ_SINGLE,
                year=_year(release.get("first-release-date")),
                source_confidence=confidence,
                notes="Official MusicBrainz release group classified as a Single.",
            )
            single_count += 1
            continue
        albums = Album.objects.filter(artist=artist)
        scored = sorted(
            ((_title_score(release_title, album.title), album) for album in albums),
            key=lambda x: x[0],
            reverse=True,
        )
        album = scored[0][1] if scored and scored[0][0] >= Decimal("0.82") else None
        if not album:
            if release.get("primary-type") == "Album":
                secondary_types = release.get("secondary-types") or []
                release_type = " / ".join(secondary_types) or "Album"
                MissingAlbum.objects.update_or_create(
                    artist=artist,
                    source=Source.MUSICBRAINZ,
                    external_id=release["id"],
                    defaults={
                        "source_record": release_record,
                        "title": release_title,
                        "normalized_title": normalize_text(release_title),
                        "year": _year(release.get("first-release-date")),
                        "release_type": release_type,
                    },
                )
            continue
        MissingAlbum.objects.filter(
            artist=artist, source=Source.MUSICBRAINZ, external_id=release["id"]
        ).delete()
        matched_albums += 1
        ExternalIdentifier.objects.update_or_create(
            source=Source.MUSICBRAINZ,
            entity_kind="album",
            external_id=release["id"],
            defaults={
                "album": album,
                "source_record": release_record,
                "confidence": scored[0][0],
                "decision": Decision.ACCEPTED,
            },
        )
        terms = release.get("genres", []) or release.get("tags", [])
        for term in sorted(terms, key=lambda x: x.get("count", 0), reverse=True)[:10]:
            name = term.get("name", "").strip()
            if not name:
                continue
            genre, _ = Genre.objects.get_or_create(
                normalized_name=normalize_text(name), defaults={"name": name}
            )
            AlbumGenreEvidence.objects.update_or_create(
                album=album,
                genre=genre,
                source=Source.MUSICBRAINZ,
                defaults={
                    "source_record": release_record,
                    "confidence": Decimal("0.8"),
                    "decision": Decision.ACCEPTED,
                },
            )
    for relation in client.relationships(mbid):
        target = relation.get("artist") or {}
        raw_name = target.get("name", "").strip()
        if not raw_name:
            continue
        relation_record = _record(
            Source.MUSICBRAINZ,
            "artist_relationship",
            f"{mbid}:{target.get('id')}:{relation.get('type-id')}",
            relation,
        )
        relation_type = (
            RelatedArtistEvidence.RelationshipType.MEMBER_OF
            if "member" in relation.get("type", "")
            else RelatedArtistEvidence.RelationshipType.COLLABORATOR
        )
        _store_related_artist_evidence(
            artist=artist,
            relationship_type=relation_type,
            source=Source.MUSICBRAINZ,
            source_record=relation_record,
            raw_name=raw_name,
            confidence=Decimal("0.9"),
        )
    return {"albums": matched_albums, "singles": single_count}


def _expanded_table_rows(table):
    """Expand row/column spans so discography columns retain their meaning."""
    carried = {}
    expanded = []
    rows = table.find_all("tr", recursive=False)
    if not rows:
        rows = [
            row
            for section in table.find_all(["thead", "tbody", "tfoot"], recursive=False)
            for row in section.find_all("tr", recursive=False)
        ]
    for row in rows:
        cells = row.find_all(["td", "th"], recursive=False)
        grid = []
        next_carried = {}
        column = 0

        def append_carried(index):
            cell, remaining = carried[index]
            grid.append(cell)
            if remaining > 1:
                next_carried[index] = (cell, remaining - 1)

        for cell in cells:
            while column in carried:
                append_carried(column)
                column += 1
            colspan = max(int(cell.get("colspan", 1) or 1), 1)
            rowspan = max(int(cell.get("rowspan", 1) or 1), 1)
            for _ in range(colspan):
                grid.append(cell)
                if rowspan > 1:
                    next_carried[column] = (cell, rowspan - 1)
                column += 1
        if carried:
            final_column = max(carried)
            while column <= final_column:
                if column in carried:
                    append_carried(column)
                else:
                    grid.append(None)
                column += 1
        carried = next_carried
        expanded.append(grid)
    return expanded


def _section_candidates(html):
    soup = BeautifulSoup(html, "html.parser")
    output = []
    kind = None
    for element in soup.find_all(["h2", "h3", "h4", "table", "ul"]):
        if element.name.startswith("h"):
            heading = normalize_text(element.get_text(" ", strip=True))
            if "single" in heading:
                kind = NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE
            elif "music video" in heading or "videography" in heading:
                kind = NoteworthyEvidence.EvidenceType.WIKIPEDIA_VIDEO
            elif element.name == "h2":
                kind = None
            elif kind == NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE and not re.fullmatch(
                r"(?:19|20)\d0 ?s(?: present)?", heading
            ):
                # A sibling subsection such as "Other appearances" or "List of
                # other songs" ends a singles table. Decade headings remain part
                # of the parent Singles section.
                kind = None
            continue
        if not kind:
            continue
        if element.find_parent("table"):
            # Certification lists and layout tables nested inside a discography
            # table are metadata, not additional song lists.
            continue
        if element.name == "table":
            title_index = None
            year_index = None
            album_index = None
            rows = _expanded_table_rows(element)
            for cells in rows:
                labels = [
                    normalize_text(cell.get_text(" ", strip=True)) if cell else "" for cell in cells
                ]
                if title_index is None:
                    title_index = next(
                        (
                            i
                            for i, label in enumerate(labels)
                            if label in {"title", "track", "single", "song"}
                        ),
                        None,
                    )
                if year_index is None:
                    year_index = next(
                        (i for i, label in enumerate(labels) if label == "year"),
                        None,
                    )
                if album_index is None:
                    album_index = next(
                        (i for i, label in enumerate(labels) if label == "album"),
                        None,
                    )
            current_year = None
            for cells in rows:
                labels = [
                    normalize_text(cell.get_text(" ", strip=True)) if cell else "" for cell in cells
                ]
                if any(label in {"title", "track", "single", "song"} for label in labels):
                    continue
                if title_index is None or title_index >= len(cells):
                    continue
                year_cell = (
                    cells[year_index]
                    if year_index is not None and year_index < len(cells)
                    else None
                )
                year = _year(_wikipedia_node_text(year_cell)) if year_cell else None
                current_year = year or current_year
                cell = cells[title_index]
                if not cell:
                    continue
                raw_title = _wikipedia_node_text(cell)
                quoted = re.search(r'["“](.*?)["”]', raw_title)
                anchor = _wikipedia_title_anchor(cell)
                title = _clean_wikipedia_title(
                    quoted.group(1)
                    if quoted
                    else (anchor.get_text(" ", strip=True) if anchor else raw_title)
                )
                album_title = ""
                if album_index is not None and album_index < len(cells) and cells[album_index]:
                    album_title = _clean_wikipedia_title(_wikipedia_node_text(cells[album_index]))
                    if normalize_text(album_title).startswith("non album"):
                        album_title = ""
                normalized_title = normalize_text(title)
                if normalized_title and normalized_title not in {
                    "title",
                    "track",
                    "single",
                    "song",
                }:
                    output.append((kind, title, current_year, album_title))
        elif element.name == "ul":
            for item in element.find_all("li", recursive=False):
                raw_title = _wikipedia_node_text(item)
                quoted = re.search(r'["“](.*?)["”]', raw_title)
                anchor = _wikipedia_title_anchor(item)
                title = _clean_wikipedia_title(
                    quoted.group(1) if quoted else (anchor.get_text(strip=True) if anchor else "")
                )
                if title:
                    output.append((kind, title, _year(raw_title), ""))
    deduplicated = {}
    for candidate in output:
        key = (normalize_text(candidate[1]), candidate[0])
        existing = deduplicated.get(key)
        if not existing:
            deduplicated[key] = candidate
            continue
        deduplicated[key] = (
            existing[0],
            existing[1],
            existing[2] or candidate[2],
            existing[3] or candidate[3],
        )
    return list(deduplicated.values())


def _merge_candidate_context(candidates):
    """Share known single/album context with video mentions of the same song."""
    album_by_title = {}
    for kind, title, year, album_title in candidates:
        key = _title_key(title)
        if album_title and (
            key not in album_by_title or kind == NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE
        ):
            album_by_title[key] = album_title

    merged = {}
    for kind, title, year, album_title in candidates:
        key = (kind, _title_key(title))
        candidate = (kind, title, year, album_title or album_by_title.get(key[1], ""))
        existing = merged.get(key)
        if not existing:
            merged[key] = candidate
        else:
            merged[key] = (
                existing[0],
                existing[1],
                existing[2] or candidate[2],
                existing[3] or candidate[3],
            )
    return list(merged.values())


def _wikipedia_infobox(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=lambda value: value and "infobox" in value)
    output = {}
    if not table:
        return output
    for row in table.find_all("tr"):
        heading = row.find("th")
        value = row.find("td")
        if not heading or not value:
            continue
        key = normalize_text(heading.get_text(" ", strip=True))
        anchors = [
            anchor.get_text(" ", strip=True)
            for anchor in value.find_all("a")
            if anchor.get_text(" ", strip=True)
        ]
        output[key] = anchors or [
            x.strip() for x in re.split(r"[,;/]", value.get_text(" ", strip=True)) if x.strip()
        ]
    return output


def _album_infobox_singles(html):
    """Read only the album infobox's formal singles list, not prose or track listings."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=lambda value: value and "infobox" in value)
    if not table:
        return []
    for heading in table.find_all(["th", "td"]):
        if not normalize_text(heading.get_text(" ", strip=True)).startswith("singles from"):
            continue
        row = heading.find_parent("tr")
        search_rows = [row] if row else []
        if row:
            search_rows.extend(row.find_next_siblings("tr", limit=2))
        for search_row in search_rows:
            items = search_row.find_all("li")
            if not items:
                continue
            singles = []
            for item in items:
                quoted = re.search(r'["“](.*?)["”]', item.get_text(" ", strip=True))
                anchor = item.find("a")
                title = (
                    quoted.group(1)
                    if quoted
                    else (anchor.get_text(" ", strip=True) if anchor else "")
                )
                if title and normalize_text(title) != "released":
                    singles.append(title.strip())
            if singles:
                return list(dict.fromkeys(singles))
    return []


def _discography_title(html):
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a"):
        label = normalize_text(anchor.get_text(" ", strip=True))
        title = anchor.get("title", "")
        href = anchor.get("href", "")
        if (
            "discography" not in label
            and "discography" not in normalize_text(title)
            and "discography" not in normalize_text(href)
        ):
            continue
        if "/wiki/" in href:
            return unquote(href.split("/wiki/", 1)[1]).replace("_", " ")
        if href.startswith("./"):
            return unquote(href[2:]).replace("_", " ")
    return None


def enrich_wikipedia(artist):
    client = WikipediaClient()
    parsed = client.page_html(artist.name)
    exact_info = _wikipedia_infobox(parsed.get("text", ""))
    is_artist_page = any(
        key in exact_info
        for key in ("members", "past members", "origin", "years active", "occupations")
    )
    if (
        parsed.get("pageid")
        and normalize_text(parsed.get("title", "")) == artist.normalized_name
        and is_artist_page
    ):
        confidence = Decimal("1")
        title = parsed["title"]
    else:
        confidence, candidate = _best_artist_candidate(
            artist.name, client.find_page(artist.name), "title"
        )
        if not candidate or confidence < Decimal("0.65"):
            return {"tracks": 0, "warning": "No confident Wikipedia page match"}
        title = candidate["title"]
        parsed = client.page_html(title)
    html = parsed.get("text", "")
    candidates = _section_candidates(html)
    discography_title = _discography_title(html)
    discography = None
    if discography_title and normalize_text(discography_title) != normalize_text(title):
        discography = client.page_html(discography_title)
        candidates.extend(_section_candidates(discography.get("text", "")))
    candidates = _merge_candidate_context(candidates)

    # Fetch the artist and linked discography pages before replacing existing
    # evidence. A provider error must leave the last successful snapshot intact.
    with transaction.atomic():
        page_record = _record(
            Source.WIKIPEDIA,
            "artist_page",
            str(parsed.get("pageid") or title),
            parsed,
            wikipedia_url(title),
        )
        ExternalIdentifier.objects.update_or_create(
            source=Source.WIKIPEDIA,
            entity_kind="artist",
            external_id=str(parsed.get("pageid") or title),
            defaults={
                "artist": artist,
                "source_record": page_record,
                "confidence": confidence,
                "decision": Decision.ACCEPTED
                if confidence >= Decimal("0.85")
                else Decision.PENDING,
            },
        )
        if discography is not None:
            _record(
                Source.WIKIPEDIA,
                "discography_page",
                str(discography.get("pageid") or discography_title),
                discography,
                wikipedia_url(discography_title),
            )
        NoteworthyEvidence.objects.filter(
            artist=artist,
            evidence_type__in=[
                NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
                NoteworthyEvidence.EvidenceType.WIKIPEDIA_VIDEO,
            ],
            decision_is_manual=False,
        ).delete()
        for kind, song_title, year, album_title in candidates:
            external_id = f"{parsed.get('pageid') or title}:{kind}:{normalize_text(song_title)}"
            record = _record(
                Source.WIKIPEDIA,
                "track_mention",
                external_id,
                {"title": song_title, "year": year, "kind": kind, "page": title},
                wikipedia_url(title),
            )
            _external_track(
                Source.WIKIPEDIA,
                record,
                artist,
                song_title,
                kind,
                year=year,
                album_title=album_title,
                source_confidence=confidence,
            )

    infobox = _wikipedia_infobox(html)
    related_count = 0
    for key in ("associated acts", "spinoffs", "spin offs"):
        for raw_name in infobox.get(key, []):
            if _store_related_artist_evidence(
                artist=artist,
                relationship_type=RelatedArtistEvidence.RelationshipType.COLLABORATOR,
                source=Source.WIKIPEDIA,
                source_record=page_record,
                raw_name=raw_name,
                confidence=Decimal("0.8"),
            ):
                related_count += 1
    album_genres = 0
    for album in artist.albums.all():
        album_confidence, album_candidate = _best_album_candidate(
            artist, album, client.find_page(f"{artist.name} {album.title} album")
        )
        if not album_candidate or album_confidence < Decimal("0.82"):
            continue
        album_title = album_candidate["title"]
        album_page = client.page_html(album_title)
        album_info = _wikipedia_infobox(album_page.get("text", ""))
        album_singles = _album_infobox_singles(album_page.get("text", ""))

        # Treat each album page as its own replaceable snapshot so a later
        # network failure cannot roll back the artist/discography evidence.
        with transaction.atomic():
            ExternalIdentifier.objects.filter(
                source=Source.WIKIPEDIA, entity_kind="album", album=album
            ).delete()
            AlbumGenreEvidence.objects.filter(album=album, source=Source.WIKIPEDIA).delete()
            album_record = _record(
                Source.WIKIPEDIA,
                "album_page",
                str(album_page.get("pageid") or album_title),
                album_page,
                wikipedia_url(album_title),
            )
            ExternalIdentifier.objects.update_or_create(
                source=Source.WIKIPEDIA,
                entity_kind="album",
                external_id=str(album_page.get("pageid") or album_title),
                defaults={
                    "album": album,
                    "source_record": album_record,
                    "confidence": album_confidence,
                    "decision": Decision.ACCEPTED
                    if album_confidence >= Decimal("0.8")
                    else Decision.PENDING,
                },
            )
            for song_title in album_singles:
                kind = NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE
                external_id = (
                    f"{album_page.get('pageid') or album_title}:{kind}:{normalize_text(song_title)}"
                )
                record = _record(
                    Source.WIKIPEDIA,
                    "track_mention",
                    external_id,
                    {"title": song_title, "kind": kind, "page": album_title},
                    wikipedia_url(album_title),
                )
                _external_track(
                    Source.WIKIPEDIA,
                    record,
                    artist,
                    song_title,
                    kind,
                    year=album.year,
                    album_title=album.title,
                    source_confidence=album_confidence,
                    notes=f"Listed in the singles infobox for {album_title}.",
                )
                candidates.append((kind, song_title, album.year, album.title))
            for genre_name in album_info.get("genre", []) + album_info.get("genres", []):
                normalized = normalize_text(genre_name)
                if not normalized:
                    continue
                genre, _ = Genre.objects.get_or_create(
                    normalized_name=normalized, defaults={"name": genre_name}
                )
                AlbumGenreEvidence.objects.update_or_create(
                    album=album,
                    genre=genre,
                    source=Source.WIKIPEDIA,
                    defaults={
                        "source_record": album_record,
                        "confidence": Decimal("0.75"),
                        "decision": Decision.ACCEPTED,
                    },
                )
                album_genres += 1
    return {
        "tracks": len(candidates),
        "album_genres": album_genres,
        "related_artists": related_count,
    }


def _youtube_title(raw_title, artist_name):
    title = re.sub(re.escape(artist_name), "", raw_title, flags=re.IGNORECASE)
    title = re.sub(
        r"[\[(].*?(official\s+)?(music\s+)?video.*?[\])]", "", title, flags=re.IGNORECASE
    )
    title = re.sub(r"\bofficial\s+(music\s+)?video\b", "", title, flags=re.IGNORECASE)
    return title.strip(" -–—|:[]()")


def _youtube_confidence(item, artist):
    snippet = item.get("snippet", {})
    description = snippet.get("description", "").casefold()
    text = f"{snippet.get('title', '')} {description}".casefold()
    channel = snippet.get("channelTitle", "").casefold()
    if any(
        term in text
        for term in ("lyric video", "official audio", "visualizer", "audio only", "fan video")
    ):
        return Decimal("0")
    legacy_vevo_release = bool(
        "vevo" in channel
        and "music video by" in description
        and "performing" in description
        and normalize_text(artist.name) in normalize_text(description)
    )
    if "official music video" not in text and not legacy_vevo_release:
        return Decimal("0")
    if normalize_text(artist.name) not in normalize_text(channel) and "vevo" not in channel:
        return Decimal("0")
    return Decimal("0.95")


@transaction.atomic
def enrich_youtube(artist):
    settings = ServiceSettings.load()
    items = YouTubeClient().search_official_videos(artist.name, settings.youtube_max_results)
    kept = 0
    for item in items:
        confidence = _youtube_confidence(item, artist)
        if confidence == 0:
            continue
        snippet = item.get("snippet", {})
        title = _youtube_title(snippet.get("title", ""), artist.name)
        record = _record(
            Source.YOUTUBE,
            "video",
            item["id"],
            item,
            f"https://www.youtube.com/watch?v={item['id']}",
        )
        external = _external_track(
            Source.YOUTUBE, record, artist, title, NoteworthyEvidence.EvidenceType.YOUTUBE_OFFICIAL
        )
        evidence = external.evidence.get(
            evidence_type=NoteworthyEvidence.EvidenceType.YOUTUBE_OFFICIAL
        )
        if evidence.decision_is_manual:
            kept += 1
            continue
        evidence.confidence = min(evidence.confidence, confidence)
        evidence.decision = (
            Decision.ACCEPTED
            if evidence.track
            and confidence >= settings.track_match_auto_accept_threshold
            and external.match_confidence >= settings.track_match_auto_accept_threshold
            else external.match_decision
        )
        evidence.notes = f"Channel: {snippet.get('channelTitle', '')}"
        evidence.save()
        kept += 1
    return {"videos": kept}


def refresh_noteworthy_decisions(
    artist=None,
    cancellation_check=None,
    expected_settings_revision=None,
):
    """Reapply automatic rules without retaining the full evidence graph in memory."""
    settings = ServiceSettings.load()
    if expected_settings_revision is None:
        expected_settings_revision = settings.noteworthy_decision_revision
    evidence_query = NoteworthyEvidence.objects.filter(decision_is_manual=False).exclude(
        evidence_type=NoteworthyEvidence.EvidenceType.MANUAL
    )
    if artist:
        evidence_query = evidence_query.filter(artist=artist)
    total = evidence_query.count()
    processed = 0
    accepted = 0
    rejected = 0
    pending = 0
    run_id = uuid.uuid4()
    stage_buffer = []
    now = timezone.now()
    NoteworthyDecisionStage.objects.filter(created_at__lt=now - timedelta(hours=24)).delete()

    def check_cancellation(**progress):
        if not cancellation_check:
            return
        try:
            cancellation_check(**progress)
        except Exception:
            NoteworthyDecisionStage.objects.filter(run_id=run_id).delete()
            raise

    check_cancellation(current=0, total=total)

    context_priority = {
        NoteworthyEvidence.EvidenceType.MUSICBRAINZ_SINGLE: 3,
        NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE: 3,
        NoteworthyEvidence.EvidenceType.SPOTIFY_TOP: 2,
    }
    artist_ids = evidence_query.order_by().values_list("artist_id", flat=True).distinct()
    for artist_id in artist_ids.iterator(chunk_size=100):
        current_artist = (
            artist if artist and artist.pk == artist_id else Artist.objects.get(pk=artist_id)
        )
        check_cancellation(current=processed, total=total)
        evidence_items = list(
            evidence_query.filter(artist_id=artist_id)
            .select_related("external_track__source_record")
            .defer("external_track__source_record__payload")
            .order_by("pk")
        )
        local_tracks = list(
            current_artist.tracks.filter(is_available=True).select_related("album").order_by("pk")
        )
        track_index = defaultdict(list)
        for local_track in local_tracks:
            key = _title_key(local_track.title)
            if key:
                track_index[key].append(local_track)

        track_context = {}
        source_names = set()
        youtube_record_ids = set()
        for item in evidence_items:
            external = item.external_track
            if not external:
                continue
            source_names.add(external.source_record.source)
            if external.source_record.source == Source.YOUTUBE:
                youtube_record_ids.add(external.source_record_id)
            if not external.album_title:
                continue
            key = _title_key(external.title)
            priority = context_priority.get(item.evidence_type, 1)
            if key not in track_context or priority > track_context[key][0]:
                track_context[key] = (
                    priority,
                    external.album_title,
                    external.year,
                )

        source_confidence = {
            row["source"]: row["best_confidence"]
            for row in ExternalIdentifier.objects.filter(
                artist_id=artist_id,
                entity_kind="artist",
                source__in=source_names,
            )
            .values("source")
            .annotate(best_confidence=Max("confidence"))
        }
        youtube_payloads = dict(
            SourceRecord.objects.filter(pk__in=youtube_record_ids).values_list("pk", "payload")
        )

        for evidence in evidence_items:
            external = evidence.external_track
            match_decision = Decision.REJECTED
            matched = None
            match_confidence = Decimal("0")
            if external:
                title = external.title
                if external.source_record.source == Source.WIKIPEDIA:
                    title = _clean_wikipedia_title(title) or title
                context = track_context.get(_title_key(title), (0, "", None))
                matched, match_confidence, match_decision = _match_local_track(
                    current_artist,
                    title,
                    year=external.year or context[2],
                    album_title=external.album_title or context[1],
                    settings=settings,
                    tracks=local_tracks,
                    track_index=track_index,
                )
                confidence = source_confidence.get(external.source_record.source)
                if (
                    match_decision == Decision.ACCEPTED
                    and confidence is not None
                    and confidence < Decimal("0.85")
                ):
                    match_decision = Decision.PENDING

            qualifies = False
            reason = "Source item did not match a local track confidently."
            if evidence.evidence_type == NoteworthyEvidence.EvidenceType.SPOTIFY_TOP:
                rank = external.rank if external else None
                qualifies = bool(rank and rank <= settings.spotify_noteworthy_max_rank)
                reason = (
                    f"Spotify artist top-track rank {rank}; automatic cutoff is "
                    f"{settings.spotify_noteworthy_max_rank}."
                )
            elif evidence.evidence_type == NoteworthyEvidence.EvidenceType.LASTFM_TOP:
                rank = external.rank if external else None
                playcount = external.playcount if external else None
                qualifies = bool(
                    rank
                    and rank <= settings.lastfm_noteworthy_max_rank
                    and playcount is not None
                    and playcount >= settings.lastfm_min_playcount
                )
                reason = (
                    f"Last.fm artist top-track rank {rank}; automatic cutoff is "
                    f"{settings.lastfm_noteworthy_max_rank}."
                )
            elif evidence.evidence_type in {
                NoteworthyEvidence.EvidenceType.MUSICBRAINZ_SINGLE,
                NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
                NoteworthyEvidence.EvidenceType.WIKIPEDIA_VIDEO,
            }:
                qualifies = True
                reason = evidence.notes or "Explicitly classified as a single by a source."
            elif evidence.evidence_type == NoteworthyEvidence.EvidenceType.YOUTUBE_OFFICIAL:
                payload = youtube_payloads.get(external.source_record_id, {}) if external else {}
                confidence = _youtube_confidence(payload, current_artist)
                qualifies = confidence >= settings.track_match_auto_accept_threshold
                reason = "Requires an explicit official music video on the artist or VEVO channel."

            if match_decision == Decision.REJECTED:
                decision = Decision.REJECTED
                rejected += 1
            elif match_decision == Decision.PENDING:
                decision = Decision.PENDING
                pending += 1
            elif qualifies:
                decision = Decision.ACCEPTED
                accepted += 1
            else:
                decision = Decision.REJECTED
                rejected += 1
            stage_buffer.append(
                NoteworthyDecisionStage(
                    run_id=run_id,
                    evidence_id=evidence.pk,
                    external_track_id=external.pk if external else None,
                    matched_track_id=matched.pk if matched else None,
                    external_title=title if external else "",
                    confidence=match_confidence,
                    decision=decision,
                    notes=reason,
                )
            )
            if len(stage_buffer) >= 250:
                NoteworthyDecisionStage.objects.bulk_create(stage_buffer, batch_size=250)
                stage_buffer.clear()
            processed += 1
            if processed % 50 == 0:
                check_cancellation(current=processed, total=total)

        # Explicitly release the artist's evidence graph and track list before
        # loading the next artist. Only a bounded staging buffer survives.
        del evidence_items, local_tracks, track_index, youtube_payloads

    if stage_buffer:
        NoteworthyDecisionStage.objects.bulk_create(stage_buffer, batch_size=250)
        stage_buffer.clear()

    try:
        with transaction.atomic():
            locked_settings = ServiceSettings.objects.select_for_update().get(pk=settings.pk)
            if locked_settings.noteworthy_decision_revision != expected_settings_revision:
                from enrichment.job_control import JobCancelled

                raise JobCancelled("Decision settings changed while reconciliation was running.")
            check_cancellation(current=total, total=total)
            last_stage_pk = 0
            while True:
                staged = list(
                    NoteworthyDecisionStage.objects.filter(
                        run_id=run_id,
                        pk__gt=last_stage_pk,
                    ).order_by("pk")[:250]
                )
                if not staged:
                    break
                last_stage_pk = staged[-1].pk
                external_objects = [
                    ExternalTrack(
                        pk=item.external_track_id,
                        matched_track_id=item.matched_track_id,
                        title=item.external_title,
                        match_confidence=item.confidence,
                        match_decision=item.decision,
                        updated_at=now,
                    )
                    for item in staged
                    if item.external_track_id
                ]
                if external_objects:
                    ExternalTrack.objects.bulk_update(
                        external_objects,
                        [
                            "matched_track",
                            "title",
                            "match_confidence",
                            "match_decision",
                            "updated_at",
                        ],
                        batch_size=250,
                    )
                evidence_objects = [
                    NoteworthyEvidence(
                        pk=item.evidence_id,
                        track_id=item.matched_track_id,
                        confidence=item.confidence,
                        decision=item.decision,
                        notes=item.notes,
                        updated_at=now,
                    )
                    for item in staged
                ]
                NoteworthyEvidence.objects.bulk_update(
                    evidence_objects,
                    ["track", "confidence", "decision", "notes", "updated_at"],
                    batch_size=250,
                )
            NoteworthyDecisionStage.objects.filter(run_id=run_id).delete()
        return {"accepted": accepted, "rejected": rejected, "pending": pending}
    finally:
        NoteworthyDecisionStage.objects.filter(run_id=run_id).delete()


def refresh_album_genres(artist=None):
    settings = ServiceSettings.load()
    from library.models import AlbumGenre

    albums = Album.objects.all()
    if artist is not None:
        albums = albums.filter(artist=artist)
    for album in albums:
        if album.genre_assignments.filter(is_manual=True).exists():
            continue
        evidence = album.genre_evidence.filter(decision=Decision.ACCEPTED).order_by(
            "-confidence", "genre__name"
        )[: settings.max_album_genres]
        AlbumGenre.objects.filter(album=album, is_manual=False).delete()
        for rank, item in enumerate(evidence, 1):
            AlbumGenre.objects.update_or_create(
                album=album,
                genre=item.genre,
                defaults={"rank": rank, "confidence": item.confidence},
            )


def _canonicalize_related_artist_evidence(local_artists):
    """Collapse legacy featured-credit relationships onto their lead artists."""
    groups = defaultdict(list)
    delete_ids = set()
    now = timezone.now()
    evidence_items = list(RelatedArtistEvidence.objects.select_related("artist").order_by("pk"))
    for evidence in evidence_items:
        source_name = primary_artist_name(evidence.artist.name)
        source_normalized = normalize_text(source_name)
        source_artist = local_artists.get(source_normalized) or evidence.artist
        related_name = primary_artist_name(evidence.related_artist_name)
        related_normalized = normalize_text(related_name)
        if not related_normalized or related_normalized == source_normalized:
            delete_ids.add(evidence.pk)
            continue
        related_artist = local_artists.get(related_normalized)
        display_name = related_artist.name if related_artist else related_name
        groups[
            (
                source_artist.pk,
                related_normalized,
                evidence.relationship_type,
                evidence.source,
            )
        ].append((evidence, source_artist, related_artist, display_name))

    updates = []
    decision_priority = {
        Decision.REJECTED: 0,
        Decision.PENDING: 1,
        Decision.ACCEPTED: 2,
    }
    for group in groups.values():
        keeper, source_artist, related_artist, display_name = min(
            group,
            key=lambda item: (
                item[0].artist_id != item[1].pk,
                item[0].related_artist_name != item[3],
                item[0].pk,
            ),
        )
        duplicates = [item[0] for item in group if item[0].pk != keeper.pk]
        delete_ids.update(item.pk for item in duplicates)
        keeper.artist = source_artist
        keeper.related_artist = related_artist
        keeper.related_artist_name = display_name
        keeper.confidence = max(item[0].confidence for item in group)
        keeper.decision = max(
            (item[0].decision for item in group),
            key=decision_priority.get,
        )
        keeper.updated_at = now
        updates.append(keeper)

    if delete_ids:
        RelatedArtistEvidence.objects.filter(pk__in=delete_ids).delete()
    if updates:
        RelatedArtistEvidence.objects.bulk_update(
            updates,
            [
                "artist",
                "related_artist",
                "related_artist_name",
                "confidence",
                "decision",
                "updated_at",
            ],
            batch_size=250,
        )


@transaction.atomic
def refresh_artist_recommendations():
    """Rank non-library artists by distinct local artists linking to them."""
    local_artists = {
        artist.normalized_name: artist
        for artist in Artist.objects.all()
        if not has_feature_credit(artist.name)
    }
    _canonicalize_related_artist_evidence(local_artists)
    buckets = {}
    evidence_items = RelatedArtistEvidence.objects.exclude(
        decision=Decision.REJECTED
    ).select_related("artist")
    for evidence in evidence_items:
        related_name = primary_artist_name(evidence.related_artist_name)
        normalized = normalize_text(related_name)
        source_name = primary_artist_name(evidence.artist.name)
        source_normalized = normalize_text(source_name)
        if not normalized or normalized == source_normalized:
            continue
        local_match = local_artists.get(normalized)
        if local_match:
            continue
        bucket = buckets.setdefault(
            normalized,
            {
                "name": related_name,
                "artists": {},
                "sources": set(),
                "types": set(),
                "evidence_count": 0,
            },
        )
        local_source = local_artists.get(source_normalized)
        bucket["artists"][source_normalized] = local_source.name if local_source else source_name
        bucket["sources"].add(evidence.source)
        bucket["types"].add(evidence.relationship_type)
        bucket["evidence_count"] += 1

    ranked = sorted(
        buckets.items(),
        key=lambda item: (
            -len(item[1]["artists"]),
            -item[1]["evidence_count"],
            item[1]["name"].casefold(),
        ),
    )
    ArtistRecommendation.objects.all().delete()
    ArtistRecommendation.objects.bulk_create(
        [
            ArtistRecommendation(
                name=data["name"],
                normalized_name=normalized,
                rank=rank,
                linked_artist_count=len(data["artists"]),
                evidence_count=data["evidence_count"],
                linked_artists=sorted(data["artists"].values(), key=str.casefold),
                sources=sorted(data["sources"]),
                relationship_types=sorted(data["types"]),
            )
            for rank, (normalized, data) in enumerate(ranked, 1)
        ]
    )
    return {
        "recommendations": len(ranked),
        "top_artist": ranked[0][1]["name"] if ranked else None,
    }
