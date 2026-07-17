import re
from decimal import Decimal
from urllib.parse import unquote

from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone
from rapidfuzz import fuzz

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
    NoteworthyEvidence,
    RelatedArtistEvidence,
    Source,
    SourceRecord,
)
from library.models import Album, Artist, Genre, ServiceSettings
from library.services import normalize_text


def _score(left, right):
    return Decimal(str(round(fuzz.WRatio(normalize_text(left), normalize_text(right)) / 100, 3)))


def _title_key(value):
    """Normalize a song/album title while ignoring common edition suffixes."""
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


def _match_local_track(artist, title, *, settings=None, tracks=None):
    settings = settings or ServiceSettings.load()
    tracks = tracks if tracks is not None else artist.tracks.filter(is_available=True)
    scored = sorted(
        ((_title_score(title, track.title), track) for track in tracks),
        key=lambda x: x[0],
        reverse=True,
    )
    if not scored:
        return None, Decimal("0"), Decision.REJECTED
    confidence, track = scored[0]
    if confidence >= settings.track_match_auto_accept_threshold:
        return track, confidence, Decision.ACCEPTED
    if confidence >= settings.track_match_review_threshold:
        return track, confidence, Decision.PENDING
    return None, confidence, Decision.REJECTED


def _external_track(source, record, artist, title, evidence_type, **data):
    matched, confidence, decision = _match_local_track(artist, title)
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
    NoteworthyEvidence.objects.update_or_create(
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
    return external


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
        artist=artist, evidence_type=NoteworthyEvidence.EvidenceType.SPOTIFY_TOP
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
        artist=artist, evidence_type=NoteworthyEvidence.EvidenceType.LASTFM_TOP
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
        name = item.get("name", "").strip()
        if not name:
            continue
        record = _record(
            Source.LASTFM,
            "related_artist",
            f"{normalize_text(artist.name)}:{normalize_text(name)}",
            item,
            item.get("url", ""),
        )
        related = Artist.objects.filter(normalized_name=normalize_text(name)).first()
        RelatedArtistEvidence.objects.update_or_create(
            artist=artist,
            related_artist_name=name,
            relationship_type=RelatedArtistEvidence.RelationshipType.SIMILAR,
            source=Source.LASTFM,
            defaults={
                "related_artist": related,
                "source_record": record,
                "confidence": Decimal(str(item.get("match") or 0.5)),
                "decision": Decision.ACCEPTED if related else Decision.PENDING,
            },
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
    MissingAlbum.objects.filter(artist=artist, source=Source.MUSICBRAINZ).delete()
    for release in client.release_groups(mbid):
        release_title = release.get("title", "").strip()
        if not release_title:
            continue
        albums = Album.objects.filter(artist=artist)
        scored = sorted(
            ((_title_score(release_title, album.title), album) for album in albums),
            key=lambda x: x[0],
            reverse=True,
        )
        album = scored[0][1] if scored and scored[0][0] >= Decimal("0.82") else None
        release_record = _record(
            Source.MUSICBRAINZ,
            "release_group",
            release["id"],
            release,
            f"https://musicbrainz.org/release-group/{release['id']}",
        )
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
        name = target.get("name", "").strip()
        if not name:
            continue
        relation_record = _record(
            Source.MUSICBRAINZ,
            "artist_relationship",
            f"{mbid}:{target.get('id')}:{relation.get('type-id')}",
            relation,
        )
        related = Artist.objects.filter(normalized_name=normalize_text(name)).first()
        relation_type = (
            RelatedArtistEvidence.RelationshipType.MEMBER_OF
            if "member" in relation.get("type", "")
            else RelatedArtistEvidence.RelationshipType.COLLABORATOR
        )
        RelatedArtistEvidence.objects.update_or_create(
            artist=artist,
            related_artist_name=name,
            relationship_type=relation_type,
            source=Source.MUSICBRAINZ,
            defaults={
                "related_artist": related,
                "source_record": relation_record,
                "confidence": Decimal("0.9"),
                "decision": Decision.ACCEPTED if related else Decision.PENDING,
            },
        )
    return {"albums": matched_albums}


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
            elif element.name in {"h2", "h3"}:
                kind = None
            continue
        if not kind:
            continue
        if element.name == "table":
            title_index = None
            header_width = 0
            rows = element.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"], recursive=False)
                labels = [normalize_text(cell.get_text(" ", strip=True)) for cell in cells]
                index = next(
                    (i for i, label in enumerate(labels) if label in {"title", "single", "song"}),
                    None,
                )
                if index is not None:
                    title_index = index
                    header_width = len(cells)
                    break
            current_year = None
            for row in rows:
                cells = row.find_all(["td", "th"], recursive=False)
                labels = [normalize_text(cell.get_text(" ", strip=True)) for cell in cells]
                if any(label in {"title", "single", "song"} for label in labels):
                    continue
                year = _year(row.get_text(" ", strip=True))
                current_year = year or current_year
                # Rowspans commonly omit a leading Year cell on subsequent singles.
                missing_leading_cells = max(0, header_width - len(cells))
                row_title_index = max(0, title_index - missing_leading_cells) if title_index is not None else None
                if row_title_index is not None and row_title_index < len(cells):
                    cell = cells[row_title_index]
                    raw_title = _wikipedia_node_text(cell)
                    quoted = re.search(r'["“](.*?)["”]', raw_title)
                    anchor = _wikipedia_title_anchor(cell)
                    title = _clean_wikipedia_title(
                        quoted.group(1)
                        if quoted
                        else (anchor.get_text(" ", strip=True) if anchor else raw_title)
                    )
                    if title and normalize_text(title) not in {"title", "single", "song"}:
                        output.append((kind, title, current_year))
        elif element.name == "ul":
            for item in element.find_all("li", recursive=False):
                raw_title = _wikipedia_node_text(item)
                quoted = re.search(r'["“](.*?)["”]', raw_title)
                anchor = _wikipedia_title_anchor(item)
                title = _clean_wikipedia_title(
                    quoted.group(1) if quoted else (anchor.get_text(strip=True) if anchor else "")
                )
                if title:
                    output.append((kind, title, _year(raw_title)))
    seen = set()
    return [
        x
        for x in output
        if (normalize_text(x[1]), x[0]) not in seen and not seen.add((normalize_text(x[1]), x[0]))
    ]


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
                title = quoted.group(1) if quoted else (
                    anchor.get_text(" ", strip=True) if anchor else ""
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
        if "discography" not in label and "discography" not in normalize_text(title):
            continue
        if title:
            return title
        href = anchor.get("href", "")
        if "/wiki/" in href:
            return unquote(href.split("/wiki/", 1)[1]).replace("_", " ")
        if href.startswith("./"):
            return unquote(href[2:]).replace("_", " ")
    return None


@transaction.atomic
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
            "decision": Decision.ACCEPTED if confidence >= Decimal("0.85") else Decision.PENDING,
        },
    )
    html = parsed.get("text", "")
    NoteworthyEvidence.objects.filter(
        artist=artist,
        evidence_type__in=[
            NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
            NoteworthyEvidence.EvidenceType.WIKIPEDIA_VIDEO,
        ],
    ).delete()
    candidates = _section_candidates(html)
    discography_title = _discography_title(html)
    if discography_title and normalize_text(discography_title) != normalize_text(title):
        discography = client.page_html(discography_title)
        _record(
            Source.WIKIPEDIA,
            "discography_page",
            str(discography.get("pageid") or discography_title),
            discography,
            wikipedia_url(discography_title),
        )
        candidates.extend(_section_candidates(discography.get("text", "")))
    for kind, song_title, year in candidates:
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
            source_confidence=confidence,
        )
    infobox = _wikipedia_infobox(html)
    related_count = 0
    for key in ("associated acts", "spinoffs", "spin offs"):
        for name in infobox.get(key, []):
            if normalize_text(name) == artist.normalized_name:
                continue
            related = Artist.objects.filter(normalized_name=normalize_text(name)).first()
            RelatedArtistEvidence.objects.update_or_create(
                artist=artist,
                related_artist_name=name,
                relationship_type=RelatedArtistEvidence.RelationshipType.COLLABORATOR,
                source=Source.WIKIPEDIA,
                defaults={
                    "related_artist": related,
                    "source_record": page_record,
                    "confidence": Decimal("0.8"),
                    "decision": Decision.ACCEPTED if related else Decision.PENDING,
                },
            )
            related_count += 1
    album_genres = 0
    for album in artist.albums.all():
        album_confidence, album_candidate = _best_album_candidate(
            artist, album, client.find_page(f"{artist.name} {album.title} album")
        )
        if not album_candidate or album_confidence < Decimal("0.82"):
            continue
        ExternalIdentifier.objects.filter(
            source=Source.WIKIPEDIA, entity_kind="album", album=album
        ).delete()
        AlbumGenreEvidence.objects.filter(album=album, source=Source.WIKIPEDIA).delete()
        album_title = album_candidate["title"]
        album_page = client.page_html(album_title)
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
        album_info = _wikipedia_infobox(album_page.get("text", ""))
        for song_title in _album_infobox_singles(album_page.get("text", "")):
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
                source_confidence=album_confidence,
                notes=f"Listed in the singles infobox for {album_title}.",
            )
            candidates.append((kind, song_title, None))
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
        evidence.confidence = min(evidence.confidence, confidence)
        evidence.decision = (
            Decision.ACCEPTED
            if evidence.track
            and confidence >= settings.youtube_auto_accept_confidence
            and external.match_confidence >= settings.track_match_auto_accept_threshold
            else external.match_decision
        )
        evidence.notes = f"Channel: {snippet.get('channelTitle', '')}"
        evidence.save()
        kept += 1
    return {"videos": kept}


@transaction.atomic
def refresh_noteworthy_decisions(artist=None):
    """Reapply automatic source rules to both new and previously stored evidence."""
    settings = ServiceSettings.load()
    evidence_items = NoteworthyEvidence.objects.exclude(
        evidence_type=NoteworthyEvidence.EvidenceType.MANUAL
    ).select_related("artist", "external_track__source_record", "track")
    if artist:
        evidence_items = evidence_items.filter(artist=artist)
    accepted = 0
    rejected = 0
    pending = 0
    changed = []
    tracks_by_artist = {}
    source_confidence_cache = {}
    now = timezone.now()
    for evidence in evidence_items:
        external = evidence.external_track
        match_decision = Decision.REJECTED
        if external:
            if external.source_record.source == Source.WIKIPEDIA:
                cleaned_title = _clean_wikipedia_title(external.title)
                if cleaned_title:
                    external.title = cleaned_title
            local_tracks = tracks_by_artist.get(evidence.artist_id)
            if local_tracks is None:
                local_tracks = list(evidence.artist.tracks.filter(is_available=True))
                tracks_by_artist[evidence.artist_id] = local_tracks
            matched, match_confidence, match_decision = _match_local_track(
                evidence.artist,
                external.title,
                settings=settings,
                tracks=local_tracks,
            )
            external.matched_track = matched
            external.match_confidence = match_confidence
            evidence.track = matched
            evidence.confidence = match_confidence
            source_key = (evidence.artist_id, external.source_record.source)
            if source_key not in source_confidence_cache:
                source_confidence_cache[source_key] = (
                    ExternalIdentifier.objects.filter(
                        artist_id=evidence.artist_id,
                        entity_kind="artist",
                        source=external.source_record.source,
                    )
                    .order_by("-confidence")
                    .values_list("confidence", flat=True)
                    .first()
                )
            source_confidence = source_confidence_cache[source_key]
            if (
                match_decision == Decision.ACCEPTED
                and source_confidence is not None
                and source_confidence < Decimal("0.85")
            ):
                match_decision = Decision.PENDING
        qualifies = False
        reason = "Source item did not match a local track confidently."
        if evidence.evidence_type == NoteworthyEvidence.EvidenceType.SPOTIFY_TOP:
            rank = external.rank if external else None
            qualifies = bool(rank and rank <= settings.spotify_noteworthy_max_rank)
            reason = f"Spotify artist top-track rank {rank}; automatic cutoff is {settings.spotify_noteworthy_max_rank}."
        elif evidence.evidence_type == NoteworthyEvidence.EvidenceType.LASTFM_TOP:
            rank = external.rank if external else None
            playcount = external.playcount if external else None
            qualifies = bool(
                rank
                and rank <= settings.lastfm_noteworthy_max_rank
                and playcount is not None
                and playcount >= settings.lastfm_min_playcount
            )
            reason = f"Last.fm artist top-track rank {rank}; automatic cutoff is {settings.lastfm_noteworthy_max_rank}."
        elif evidence.evidence_type in {
            NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
            NoteworthyEvidence.EvidenceType.WIKIPEDIA_VIDEO,
        }:
            qualifies = True
            reason = evidence.notes or "Explicitly listed by Wikipedia."
        elif evidence.evidence_type == NoteworthyEvidence.EvidenceType.YOUTUBE_OFFICIAL:
            payload = external.source_record.payload if external else {}
            confidence = _youtube_confidence(payload, evidence.artist)
            qualifies = confidence >= settings.youtube_auto_accept_confidence
            reason = "Requires an explicit official music video on the artist or VEVO channel."

        if match_decision == Decision.REJECTED:
            evidence.decision = Decision.REJECTED
            rejected += 1
        elif match_decision == Decision.PENDING:
            evidence.decision = Decision.PENDING
            pending += 1
        elif qualifies:
            evidence.decision = Decision.ACCEPTED
            accepted += 1
        else:
            evidence.decision = Decision.REJECTED
            rejected += 1
        if external:
            external.match_decision = evidence.decision
            external.save(
                update_fields=[
                    "matched_track",
                    "title",
                    "match_confidence",
                    "match_decision",
                    "updated_at",
                ]
            )
        evidence.notes = reason
        evidence.updated_at = now
        changed.append(evidence)
    NoteworthyEvidence.objects.bulk_update(
        changed, ["track", "confidence", "decision", "notes", "updated_at"], batch_size=250
    )
    return {"accepted": accepted, "rejected": rejected, "pending": pending}


def refresh_album_genres():
    settings = ServiceSettings.load()
    from library.models import AlbumGenre

    for album in Album.objects.all():
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


@transaction.atomic
def refresh_artist_recommendations():
    """Rank non-library artists by distinct local artists linking to them."""
    local_artists = {artist.normalized_name: artist for artist in Artist.objects.all()}
    buckets = {}
    reconciled = []
    evidence_items = RelatedArtistEvidence.objects.exclude(decision=Decision.REJECTED).select_related(
        "artist"
    )
    for evidence in evidence_items:
        normalized = normalize_text(evidence.related_artist_name)
        if not normalized or normalized == evidence.artist.normalized_name:
            continue
        local_match = local_artists.get(normalized)
        if local_match:
            if evidence.related_artist_id != local_match.pk:
                evidence.related_artist = local_match
                reconciled.append(evidence)
            continue
        bucket = buckets.setdefault(
            normalized,
            {
                "name": evidence.related_artist_name,
                "artists": {},
                "sources": set(),
                "types": set(),
                "evidence_count": 0,
            },
        )
        bucket["artists"][evidence.artist.normalized_name] = evidence.artist.name
        bucket["sources"].add(evidence.source)
        bucket["types"].add(evidence.relationship_type)
        bucket["evidence_count"] += 1

    if reconciled:
        RelatedArtistEvidence.objects.bulk_update(reconciled, ["related_artist"])

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
