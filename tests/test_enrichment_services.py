from datetime import timedelta
from decimal import Decimal

import pytest
import requests
from django.utils import timezone

from enrichment.clients import (
    BaseClient,
    MusicBrainzClient,
    ProviderNotConfigured,
    RateLimited,
)
from enrichment.job_control import JobCancelled
from enrichment.models import (
    ArtistRecommendation,
    Decision,
    ExternalTrack,
    MissingAlbum,
    NoteworthyEvidence,
    RelatedArtistEvidence,
    Source,
    SourceRecord,
)
from enrichment.services import (
    _album_infobox_singles,
    _best_album_candidate,
    _discography_title,
    _match_local_track,
    _merge_candidate_context,
    _section_candidates,
    _wikipedia_infobox,
    _youtube_confidence,
    _youtube_title,
    enrich_musicbrainz,
    enrich_spotify,
    enrich_wikipedia,
    refresh_artist_recommendations,
    refresh_noteworthy_decisions,
)
from library.models import Album, Artist, ServiceSettings, Track
from library.services import normalize_text
from playlists.services import noteworthy_tracks


@pytest.mark.django_db
def test_fuzzy_match_accepts_version_suffix(track, artist):
    matched, confidence, decision = _match_local_track(
        artist, "Change (In the House of Flies) - Radio Edit"
    )
    assert matched == track
    assert confidence >= Decimal("0.9")
    assert decision == Decision.ACCEPTED


@pytest.mark.django_db
def test_track_match_rejects_unrelated_partial_substring(track, artist):
    track.title = "Into the Great Wide Open"
    track.normalized_title = normalize_text(track.title)
    track.save()

    matched, confidence, decision = _match_local_track(artist, "Room at the Top")

    assert matched is None
    assert confidence < Decimal("0.85")
    assert decision == Decision.REJECTED


@pytest.mark.django_db
def test_track_match_sends_only_close_titles_to_review(track, artist):
    matched, confidence, decision = _match_local_track(artist, "Change in House of Fire")

    assert matched == track
    assert Decimal("0.85") <= confidence < Decimal("0.9")
    assert decision == Decision.PENDING


def test_wikipedia_parser_reads_single_and_video_sections():
    html = """
    <h2>Singles</h2><table><tr><th>Year</th><th>Title</th></tr>
    <tr><td>2000</td><td>\"Change\"</td></tr></table>
    <h2>Music videos</h2><ul><li>2001 – \"Back to School\"</li></ul>
    """
    values = _section_candidates(html)
    assert ("wikipedia_single", "Change", 2000, "") in values
    assert ("wikipedia_video", "Back to School", 2001, "") in values


def test_wikipedia_parser_removes_reference_markup_and_unmatched_quote():
    html = """
    <h2>Music videos</h2><table>
      <tr><th>Year</th><th>Title</th></tr>
      <tr><td>2005</td><td>Image of the Invisible" <sup class="reference"><a href="./cite_note-33">[ 33 ]</a></sup></td></tr>
      <tr><td>2008</td><td>"Come All You Weary"<sup class="reference"><a href="./cite_note-36">[36]</a></sup></td></tr>
    </table>
    """

    assert _section_candidates(html) == [
        ("wikipedia_video", "Image of the Invisible", 2005, ""),
        ("wikipedia_video", "Come All You Weary", 2008, ""),
    ]


def test_wikipedia_parser_keeps_non_album_singles_in_rowspan_tables():
    html = """
    <h2>Discography</h2><h3>Singles</h3>
    <table class="wikitable">
      <tr><th>Year</th><th>Title</th><th>Album</th></tr>
      <tr><td rowspan="2">2025</td><td>"Album Single"</td><td>Studio Album</td></tr>
      <tr><td>"Standalone Single"</td><td>Non-album single</td></tr>
    </table>
    """
    values = _section_candidates(html)
    assert ("wikipedia_single", "Album Single", 2025, "Studio Album") in values
    assert ("wikipedia_single", "Standalone Single", 2025, "") in values
    assert all(title != "Non-album single" for _, title, _, _ in values)


def test_wikipedia_parser_keeps_decade_subsections_and_rowspan_album_context():
    html = """
    <h2>Singles</h2>
    <h3>2000's</h3>
    <table class="wikitable">
      <tr><th>Title</th><th>Year</th><th>Peak</th><th>Album</th></tr>
      <tr><td>"Wasteland"</td><td rowspan="3">2005</td><td>1</td><td rowspan="3">The Autumn Effect</td></tr>
      <tr><td>"Through the Iris"</td><td>35</td></tr>
      <tr><td>"Waking Up"</td><td>32</td></tr>
    </table>
    <h3>2010's</h3>
    <table class="wikitable">
      <tr><th>Title</th><th>Year</th><th>Peak</th><th>Album</th></tr>
      <tr><td>"Shoot It Out"</td><td>2010</td><td>6</td><td rowspan="2">Feeding the Wolves</td></tr>
      <tr><td>"Fix Me"</td><td>2011</td><td>10</td></tr>
    </table>
    """

    assert _section_candidates(html) == [
        ("wikipedia_single", "Wasteland", 2005, "The Autumn Effect"),
        ("wikipedia_single", "Through the Iris", 2005, "The Autumn Effect"),
        ("wikipedia_single", "Waking Up", 2005, "The Autumn Effect"),
        ("wikipedia_single", "Shoot It Out", 2010, "Feeding the Wolves"),
        ("wikipedia_single", "Fix Me", 2011, "Feeding the Wolves"),
    ]


def test_wikipedia_parser_stops_before_submersed_other_songs():
    html = """
    <h2>Discography</h2>
    <h3>Singles</h3>
    <table>
      <tr><th>Year</th><th>Title</th><th>Album</th></tr>
      <tr><td>2003</td><td>"You Run"</td><td>In Due Time</td></tr>
      <tr><td>2004</td><td>"Hollow"</td><td>In Due Time</td></tr>
    </table>
    <h3>List of other songs</h3>
    <ul>
      <li>"Complicated" featured in a soundtrack</li>
      <li>"Broken Man" from an unreleased album</li>
    </ul>
    """

    assert _section_candidates(html) == [
        ("wikipedia_single", "You Run", 2003, "In Due Time"),
        ("wikipedia_single", "Hollow", 2004, "In Due Time"),
    ]


def test_wikipedia_parser_stops_before_thursday_other_appearances():
    html = """
    <h2>Songs</h2>
    <h3>Singles</h3>
    <table>
      <tr><th>Title</th><th>Year</th><th>Album</th></tr>
      <tr><td>"Understanding in a Car Crash"</td><td>2001</td>
          <td>Full Collapse</td></tr>
      <tr><td>"Cross Out the Eyes"</td><td>2002</td><td>Full Collapse</td></tr>
    </table>
    <h3>Other appearances</h3>
    <table>
      <tr><th>Title</th><th>Year</th><th>Album</th></tr>
      <tr><td>"Ian Curtis"</td><td>2000</td><td>Status 12</td></tr>
      <tr><td>"Rape Me"</td><td>2014</td><td>Tribute album</td></tr>
    </table>
    """

    assert _section_candidates(html) == [
        ("wikipedia_single", "Understanding in a Car Crash", 2001, "Full Collapse"),
        ("wikipedia_single", "Cross Out the Eyes", 2002, "Full Collapse"),
    ]


def test_wikipedia_single_album_context_is_applied_to_matching_video():
    candidates = [
        ("wikipedia_single", "Wasteland", 2005, "The Autumn Effect"),
        ("wikipedia_video", "Wasteland", 2004, ""),
    ]

    assert _merge_candidate_context(candidates) == [
        ("wikipedia_single", "Wasteland", 2005, "The Autumn Effect"),
        ("wikipedia_video", "Wasteland", 2004, "The Autumn Effect"),
    ]


def test_wikipedia_discography_link_ignores_section_edit_control():
    html = """
    <h2>
      Discography
      <span class="mw-editsection">
        <a href="/w/index.php?title=10_Years_(band)&action=edit&section=17"
           title="Edit section: Discography">edit</a>
      </span>
    </h2>
    <p>Main article:
      <a href="/wiki/10_Years_discography" title="10 Years discography">
        10 Years discography
      </a>
    </p>
    """

    assert _discography_title(html) == "10 Years discography"


@pytest.mark.django_db
def test_wikipedia_enrichment_reads_singles_from_linked_discography(root, monkeypatch):
    artist = Artist.objects.create(
        name="10 Years",
        sort_name="10 Years",
        normalized_name=normalize_text("10 Years"),
    )
    album = Album.objects.create(
        artist=artist,
        title="The Autumn Effect",
        normalized_title=normalize_text("The Autumn Effect"),
        year=2005,
    )
    local_track = Track.objects.create(
        library_root=root,
        artist=artist,
        album=album,
        full_path=f"{root.path}/Wasteland.mp3",
        relative_path="Wasteland.mp3",
        file_format="mp3",
        title="Wasteland",
        normalized_title=normalize_text("Wasteland"),
        year=2005,
        duration_seconds=Decimal("240"),
        file_size=100,
        file_modified_ns=1,
    )

    class FakeWikipedia:
        def page_html(self, title):
            if title == "10 Years":
                return {
                    "pageid": 1,
                    "title": "10 Years",
                    "text": '<div class="redirectMsg">Redirect to 10 years</div>',
                }
            if title == "10 Years (band)":
                return {
                    "pageid": 2,
                    "title": title,
                    "text": """
                      <table class="infobox">
                        <tr><th>Origin</th><td>Knoxville, Tennessee</td></tr>
                      </table>
                      <h2>Discography
                        <a href="/w/index.php?title=10_Years_(band)&action=edit&section=17"
                           title="Edit section: Discography">edit</a>
                      </h2>
                      <p><a href="/wiki/10_Years_discography"
                            title="10 Years discography">10 Years discography</a></p>
                    """,
                }
            if title == "10 Years discography":
                return {
                    "pageid": 3,
                    "title": title,
                    "text": """
                      <h2>Singles</h2>
                      <table>
                        <tr><th>Title</th><th>Year</th><th>Album</th></tr>
                        <tr><td>"Wasteland"</td><td>2005</td>
                            <td>The Autumn Effect</td></tr>
                        <tr><td>"Through the Iris"</td><td>2006</td>
                            <td>The Autumn Effect</td></tr>
                      </table>
                    """,
                }
            raise AssertionError(f"Unexpected Wikipedia page: {title}")

        def find_page(self, query):
            if query == "10 Years":
                return [
                    {
                        "title": "10 Years (band)",
                        "snippet": "American alternative metal band",
                    }
                ]
            return []

    monkeypatch.setattr("enrichment.services.WikipediaClient", FakeWikipedia)

    result = enrich_wikipedia(artist)

    assert result["tracks"] == 2
    evidence = NoteworthyEvidence.objects.filter(
        artist=artist,
        evidence_type=NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
    )
    assert set(evidence.values_list("external_track__title", flat=True)) == {
        "Through the Iris",
        "Wasteland",
    }
    assert evidence.get(external_track__title="Wasteland").track == local_track


@pytest.mark.django_db
def test_wikipedia_keeps_previous_evidence_when_discography_fetch_is_rate_limited(
    artist, monkeypatch
):
    class InitialWikipedia:
        def page_html(self, title):
            return {
                "pageid": 10,
                "title": artist.name,
                "text": """
                  <table class="infobox"><tr><th>Origin</th><td>Sacramento</td></tr></table>
                  <h2>Singles</h2>
                  <table><tr><th>Title</th></tr><tr><td>"Existing Single"</td></tr></table>
                """,
            }

        def find_page(self, query):
            return []

    monkeypatch.setattr("enrichment.services.WikipediaClient", InitialWikipedia)
    enrich_wikipedia(artist)
    assert (
        NoteworthyEvidence.objects.filter(
            artist=artist,
            evidence_type=NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
        ).count()
        == 1
    )

    class RateLimitedWikipedia:
        def page_html(self, title):
            if title == artist.name:
                return {
                    "pageid": 10,
                    "title": artist.name,
                    "text": """
                      <table class="infobox">
                        <tr><th>Origin</th><td>Sacramento</td></tr>
                      </table>
                      <p><a href="/wiki/Deftones_discography"
                            title="Deftones discography">Discography</a></p>
                    """,
                }
            raise RateLimited("wikipedia", 60, "Wikipedia is cooling down")

        def find_page(self, query):
            return []

    monkeypatch.setattr("enrichment.services.WikipediaClient", RateLimitedWikipedia)

    with pytest.raises(RateLimited):
        enrich_wikipedia(artist)

    assert NoteworthyEvidence.objects.filter(
        artist=artist,
        evidence_type=NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
        external_track__title="Existing Single",
    ).exists()


@pytest.mark.django_db
def test_wikipedia_commits_discography_evidence_before_album_rate_limit(
    artist, album, track, monkeypatch
):
    class RateLimitedAlbumWikipedia:
        def page_html(self, title):
            if title == artist.name:
                return {
                    "pageid": 20,
                    "title": artist.name,
                    "text": f"""
                      <table class="infobox">
                        <tr><th>Origin</th><td>Sacramento</td></tr>
                      </table>
                      <h2>Singles</h2>
                      <table>
                        <tr><th>Title</th><th>Year</th><th>Album</th></tr>
                        <tr><td>"{track.title}"</td><td>{album.year}</td>
                            <td>{album.title}</td></tr>
                      </table>
                    """,
                }
            raise RateLimited("wikipedia", 60, "Wikipedia is cooling down")

        def find_page(self, query):
            return [
                {
                    "title": f"{album.title} (album)",
                    "snippet": f"album by {artist.name}",
                }
            ]

    monkeypatch.setattr("enrichment.services.WikipediaClient", RateLimitedAlbumWikipedia)

    with pytest.raises(RateLimited):
        enrich_wikipedia(artist)

    evidence = NoteworthyEvidence.objects.get(
        artist=artist,
        evidence_type=NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE,
        external_track__title=track.title,
    )
    assert evidence.track == track
    assert evidence.decision == Decision.ACCEPTED


@pytest.mark.django_db
def test_duplicate_track_title_prefers_source_album_then_closest_year(artist, root):
    older_album = Album.objects.create(
        artist=artist,
        title="Killing All That Holds You",
        normalized_title=normalize_text("Killing All That Holds You"),
        year=2004,
    )
    canonical_album = Album.objects.create(
        artist=artist,
        title="The Autumn Effect",
        normalized_title=normalize_text("The Autumn Effect"),
        year=2005,
    )
    older_track = Track.objects.create(
        library_root=root,
        artist=artist,
        album=older_album,
        full_path=f"{root.path}/older.mp3",
        relative_path="older.mp3",
        file_format="mp3",
        title="Wasteland",
        normalized_title="wasteland",
        duration_seconds=Decimal("240"),
        file_size=1,
        file_modified_ns=1,
    )
    canonical_track = Track.objects.create(
        library_root=root,
        artist=artist,
        album=canonical_album,
        full_path=f"{root.path}/canonical.mp3",
        relative_path="canonical.mp3",
        file_format="mp3",
        title="Wasteland",
        normalized_title="wasteland",
        duration_seconds=Decimal("240"),
        file_size=1,
        file_modified_ns=2,
    )

    matched, confidence, decision = _match_local_track(
        artist,
        "Wasteland",
        year=2005,
        album_title="The Autumn Effect",
    )
    assert matched == canonical_track
    assert matched != older_track
    assert confidence == Decimal("1")
    assert decision == Decision.ACCEPTED


def test_youtube_rejects_lyrics_and_extracts_title():
    item = {
        "snippet": {
            "title": "Deftones - Change (Official Lyric Video)",
            "description": "",
            "channelTitle": "Deftones",
        }
    }
    assert _youtube_confidence(item, type("Artist", (), {"name": "Deftones"})()) == 0
    assert _youtube_title("Deftones - Change (Official Music Video)", "Deftones") == "Change"


def test_wikipedia_infobox_preserves_album_genres_and_associated_acts():
    html = """
    <table class="infobox vevent"><tr><th>Genre</th><td><a>Alternative metal</a><br><a>Art rock</a></td></tr>
    <tr><th>Associated acts</th><td><a>Team Sleep</a><a>Crosses</a></td></tr></table>
    """
    info = _wikipedia_infobox(html)
    assert info["genre"] == ["Alternative metal", "Art rock"]
    assert info["associated acts"] == ["Team Sleep", "Crosses"]


def test_album_infobox_extracts_only_formal_singles():
    html = """
    <table class="infobox"><tr><th>Singles from <i>Team Sleep</i></th></tr>
    <tr><td><ol><li>"Ever (Foreign Flag)"<br><span>Released: April 25, 2005</span></li></ol></td></tr>
    <tr><th>Track listing</th></tr><tr><td><ol><li>Blvd. Nights</li></ol></td></tr></table>
    """
    assert _album_infobox_singles(html) == ["Ever (Foreign Flag)"]


@pytest.mark.django_db
def test_album_match_rejects_similarly_named_wrong_artist(artist):
    album = Album.objects.create(
        artist=artist, title="Deftones", normalized_title="deftones", year=2003
    )
    candidates = [
        {"title": "Armor for Sleep", "snippet": "American rock band"},
        {"title": "Deftones (album)", "snippet": "album by Deftones"},
    ]
    confidence, candidate = _best_album_candidate(artist, album, candidates)
    assert candidate["title"] == "Deftones (album)"
    assert confidence == Decimal("1")


@pytest.mark.django_db
def test_wikipedia_uses_exact_artist_page_when_search_omits_it(artist, monkeypatch):
    class FakeWikipedia:
        def page_html(self, title):
            return {
                "pageid": 42,
                "title": artist.name,
                "text": '<table class="infobox"><tr><th>Origin</th><td>Sacramento</td></tr></table>',
            }

        def find_page(self, title):
            raise AssertionError("Exact artist page should avoid unreliable search results")

    monkeypatch.setattr("enrichment.services.WikipediaClient", FakeWikipedia)
    assert enrich_wikipedia(artist)["tracks"] == 0


@pytest.mark.django_db
def test_musicbrainz_catalog_records_missing_albums_and_single_evidence(
    artist, album, track, monkeypatch
):
    class FakeMusicBrainz:
        def find_artist(self, name):
            return [{"id": "artist-1", "name": name}]

        def release_groups(self, artist_mbid):
            return [
                {
                    "id": "present",
                    "title": album.title,
                    "primary-type": "Album",
                    "first-release-date": "2000-06-20",
                },
                {
                    "id": "missing",
                    "title": "Saturday Night Wrist",
                    "primary-type": "Album",
                    "first-release-date": "2006-10-31",
                    "secondary-types": [],
                },
                {
                    "id": "single",
                    "title": track.title,
                    "primary-type": "Single",
                    "first-release-date": "2000",
                },
            ]

        def relationships(self, artist_mbid):
            return []

    monkeypatch.setattr("enrichment.services.MusicBrainzClient", FakeMusicBrainz)

    assert enrich_musicbrainz(artist) == {"albums": 1, "singles": 1}
    missing = MissingAlbum.objects.get()
    assert (missing.title, missing.year, missing.release_type) == (
        "Saturday Night Wrist",
        2006,
        "Album",
    )
    single = NoteworthyEvidence.objects.get(
        evidence_type=NoteworthyEvidence.EvidenceType.MUSICBRAINZ_SINGLE
    )
    assert single.track == track
    assert single.decision == Decision.ACCEPTED


def test_youtube_requires_explicit_official_music_video_and_artist_channel():
    artist = type("Artist", (), {"name": "Team Sleep"})()
    plain_upload = {
        "snippet": {
            "title": "Team Sleep - Blvd. Nights",
            "description": "",
            "channelTitle": "Team Sleep",
        }
    }
    official = {
        "snippet": {
            "title": "Team Sleep - Blvd. Nights (Official Music Video)",
            "description": "",
            "channelTitle": "Team Sleep",
        }
    }
    assert _youtube_confidence(plain_upload, artist) == 0
    assert _youtube_confidence(official, artist) == Decimal("0.95")


def test_youtube_accepts_legacy_vevo_music_video_description():
    artist = type("Artist", (), {"name": "Thrice"})()
    legacy_vevo = {
        "snippet": {
            "title": "Thrice - Image Of The Invisible",
            "description": "Music video by Thrice performing Image Of The Invisible. (C) 2005 Island",
            "channelTitle": "ThriceVEVO",
        }
    }
    unrelated_upload = {
        "snippet": {
            "title": "Thrice - Image Of The Invisible",
            "description": "Live video filmed on tour",
            "channelTitle": "MusicArchive",
        }
    }

    assert _youtube_confidence(legacy_vevo, artist) == Decimal("0.95")
    assert _youtube_confidence(unrelated_upload, artist) == Decimal("0")


@pytest.mark.django_db
def test_noteworthy_union_uses_top_two_per_popularity_source_and_wikipedia_single(
    artist, album, root
):
    titles = ["No One Loves Me", "New Fang", "Dead End Friends", "Scumbag Blues", "Mind Eraser"]
    tracks = {}
    for index, title in enumerate(titles, 1):
        tracks[title] = Track.objects.create(
            library_root=root,
            artist=artist,
            album=album,
            full_path=f"{root.path}/{index}.mp3",
            relative_path=f"{index}.mp3",
            file_format="mp3",
            title=title,
            normalized_title=normalize_text(title),
            duration_seconds=Decimal("240"),
            file_size=100,
            file_modified_ns=index,
        )

    def evidence(source, evidence_type, title, rank=None, playcount=None):
        record = SourceRecord.objects.create(
            source=source,
            entity_kind="track",
            external_id=f"{source}:{title}",
            fetched_at=__import__("django.utils.timezone", fromlist=["now"]).now(),
            payload={"title": title},
        )
        external = ExternalTrack.objects.create(
            source_record=record,
            artist=artist,
            matched_track=tracks[title],
            artist_name=artist.name,
            title=title,
            rank=rank,
            playcount=playcount,
            match_confidence=Decimal("1"),
            match_decision=Decision.ACCEPTED,
        )
        NoteworthyEvidence.objects.create(
            artist=artist,
            track=tracks[title],
            external_track=external,
            evidence_type=evidence_type,
            confidence=Decimal("1"),
            decision=Decision.ACCEPTED,
        )

    for rank, title in enumerate(
        ["No One Loves Me", "New Fang", "Dead End Friends", "Scumbag Blues"], 1
    ):
        evidence(Source.SPOTIFY, NoteworthyEvidence.EvidenceType.SPOTIFY_TOP, title, rank=rank)
    for rank, title in enumerate(
        ["New Fang", "Dead End Friends", "Scumbag Blues", "No One Loves Me"], 1
    ):
        evidence(
            Source.LASTFM,
            NoteworthyEvidence.EvidenceType.LASTFM_TOP,
            title,
            rank=rank,
            playcount=10000,
        )
    evidence(Source.WIKIPEDIA, NoteworthyEvidence.EvidenceType.WIKIPEDIA_SINGLE, "Mind Eraser")

    refresh_noteworthy_decisions(artist)
    assert {track.title for track in noteworthy_tracks(artist)} == {
        "No One Loves Me",
        "New Fang",
        "Dead End Friends",
        "Mind Eraser",
    }


@pytest.mark.django_db
def test_refresh_accepts_exact_titles_instead_of_retaining_stale_confidence(track, artist):
    now = __import__("django.utils.timezone", fromlist=["now"]).now()
    youtube_record = SourceRecord.objects.create(
        source=Source.YOUTUBE,
        entity_kind="video",
        external_id="exact-youtube",
        fetched_at=now,
        payload={
            "snippet": {
                "title": f"{artist.name} - {track.title} (Official Music Video)",
                "description": "",
                "channelTitle": artist.name,
            }
        },
    )
    wikipedia_record = SourceRecord.objects.create(
        source=Source.WIKIPEDIA,
        entity_kind="track_mention",
        external_id="malformed-wikipedia-title",
        fetched_at=now,
        payload={"title": f'{track.title}" [ 33 ]'},
    )
    cases = [
        (
            youtube_record,
            track.title,
            NoteworthyEvidence.EvidenceType.YOUTUBE_OFFICIAL,
            Decimal("0.450"),
        ),
        (
            wikipedia_record,
            f'{track.title}" [ 33 ]',
            NoteworthyEvidence.EvidenceType.WIKIPEDIA_VIDEO,
            Decimal("0.936"),
        ),
    ]
    for record, title, evidence_type, stale_confidence in cases:
        external = ExternalTrack.objects.create(
            source_record=record,
            artist=artist,
            matched_track=track,
            artist_name=artist.name,
            title=title,
            match_confidence=stale_confidence,
            match_decision=Decision.PENDING,
        )
        NoteworthyEvidence.objects.create(
            artist=artist,
            track=track,
            external_track=external,
            evidence_type=evidence_type,
            confidence=stale_confidence,
            decision=Decision.PENDING,
        )

    assert refresh_noteworthy_decisions(artist) == {
        "accepted": 2,
        "rejected": 0,
        "pending": 0,
    }
    for evidence in NoteworthyEvidence.objects.all():
        assert evidence.decision == Decision.ACCEPTED
        assert evidence.confidence == Decimal("1")
        assert evidence.external_track.match_confidence == Decimal("1")
    assert ExternalTrack.objects.get(source_record=wikipedia_record).title == track.title


@pytest.mark.django_db
def test_refresh_moves_automatic_decisions_with_global_threshold_and_preserves_manual(
    track, artist
):
    track.title = "Lost in a Fantasy"
    track.normalized_title = normalize_text(track.title)
    track.save(update_fields=["title", "normalized_title", "updated_at"])
    record = SourceRecord.objects.create(
        source=Source.LASTFM,
        entity_kind="track",
        external_id="lastfm-lost-in-fantasy",
        fetched_at=timezone.now(),
    )
    external = ExternalTrack.objects.create(
        source_record=record,
        artist=artist,
        matched_track=track,
        artist_name=artist.name,
        title="Lost in Fantasy",
        rank=1,
        playcount=10000,
        match_confidence=Decimal("0.938"),
        match_decision=Decision.PENDING,
    )
    evidence = NoteworthyEvidence.objects.create(
        artist=artist,
        track=track,
        external_track=external,
        evidence_type=NoteworthyEvidence.EvidenceType.LASTFM_TOP,
        confidence=Decimal("0.938"),
        decision=Decision.PENDING,
    )
    settings = ServiceSettings.load()
    settings.track_match_auto_accept_threshold = Decimal("0.900")
    settings.save()

    refresh_noteworthy_decisions(artist)
    evidence.refresh_from_db()
    assert evidence.confidence == Decimal("0.938")
    assert evidence.decision == Decision.ACCEPTED

    settings.track_match_auto_accept_threshold = Decimal("0.950")
    settings.save()
    refresh_noteworthy_decisions(artist)
    evidence.refresh_from_db()
    assert evidence.decision == Decision.PENDING

    evidence.decision = Decision.REJECTED
    evidence.decision_is_manual = True
    evidence.save(update_fields=["decision", "decision_is_manual", "updated_at"])
    settings.track_match_auto_accept_threshold = Decimal("0.900")
    settings.save()

    assert refresh_noteworthy_decisions(artist) == {
        "accepted": 0,
        "rejected": 0,
        "pending": 0,
    }
    evidence.refresh_from_db()
    assert evidence.decision == Decision.REJECTED


@pytest.mark.django_db
def test_cancelled_refresh_does_not_commit_partial_decisions(track, artist):
    record = SourceRecord.objects.create(
        source=Source.LASTFM,
        entity_kind="track",
        external_id="cancelled-confidence-refresh",
        fetched_at=timezone.now(),
    )
    external = ExternalTrack.objects.create(
        source_record=record,
        artist=artist,
        matched_track=track,
        artist_name=artist.name,
        title=track.title,
        rank=1,
        playcount=10000,
        match_confidence=Decimal("0.500"),
        match_decision=Decision.PENDING,
    )
    evidence = NoteworthyEvidence.objects.create(
        artist=artist,
        track=track,
        external_track=external,
        evidence_type=NoteworthyEvidence.EvidenceType.LASTFM_TOP,
        confidence=Decimal("0.500"),
        decision=Decision.PENDING,
    )

    def cancel_before_commit(*, current, total):
        if current == total:
            raise JobCancelled("Superseded by newer settings")

    with pytest.raises(JobCancelled):
        refresh_noteworthy_decisions(
            artist,
            cancellation_check=cancel_before_commit,
        )

    evidence.refresh_from_db()
    external.refresh_from_db()
    assert evidence.confidence == Decimal("0.500")
    assert evidence.decision == Decision.PENDING
    assert external.match_confidence == Decimal("0.500")
    assert external.match_decision == Decision.PENDING


@pytest.mark.django_db
def test_review_threshold_moves_automatic_matches_between_rejected_and_pending(track, artist):
    record = SourceRecord.objects.create(
        source=Source.LASTFM,
        entity_kind="track",
        external_id="review-threshold-match",
        fetched_at=timezone.now(),
    )
    external = ExternalTrack.objects.create(
        source_record=record,
        artist=artist,
        artist_name=artist.name,
        title="Change in House of Fire",
        rank=1,
        playcount=10000,
        match_confidence=Decimal("0.863"),
        match_decision=Decision.REJECTED,
    )
    evidence = NoteworthyEvidence.objects.create(
        artist=artist,
        external_track=external,
        evidence_type=NoteworthyEvidence.EvidenceType.LASTFM_TOP,
        confidence=Decimal("0.863"),
        decision=Decision.REJECTED,
    )
    settings = ServiceSettings.load()
    settings.track_match_review_threshold = Decimal("0.880")
    settings.track_match_auto_accept_threshold = Decimal("0.900")
    settings.save()

    refresh_noteworthy_decisions(artist)
    evidence.refresh_from_db()
    assert evidence.decision == Decision.REJECTED
    assert evidence.track is None

    settings.track_match_review_threshold = Decimal("0.850")
    settings.save()
    refresh_noteworthy_decisions(artist)
    evidence.refresh_from_db()
    assert evidence.confidence == Decimal("0.863")
    assert evidence.decision == Decision.PENDING
    assert evidence.track == track


@pytest.mark.django_db
def test_spotify_ranking_is_stored_as_independent_source_evidence(track, artist, monkeypatch):
    class FakeSpotify:
        def find_artist(self, name):
            return [
                {
                    "id": "artist-1",
                    "name": name,
                    "external_urls": {"spotify": "https://spotify/artist-1"},
                }
            ]

        def top_tracks(self, artist_id, market):
            return [
                {
                    "id": "track-1",
                    "name": track.title,
                    "duration_ms": 240000,
                    "popularity": 77,
                    "artists": [{"name": artist.name}],
                    "album": {"name": track.album.title},
                    "external_urls": {"spotify": "https://spotify/track-1"},
                }
            ]

    monkeypatch.setattr("enrichment.services.SpotifyClient", FakeSpotify)
    assert enrich_spotify(artist) == {"tracks": 1}
    record = SourceRecord.objects.get(source=Source.SPOTIFY, entity_kind="track")
    evidence = NoteworthyEvidence.objects.get(external_track__source_record=record)
    assert evidence.decision == Decision.ACCEPTED
    assert evidence.external_track.rank == 1
    assert evidence.external_track.popularity == 77
    assert evidence.external_track.playcount is None


@pytest.mark.django_db
def test_uncertain_spotify_artist_match_cannot_auto_accept_track(track, artist, monkeypatch):
    class FakeSpotify:
        def find_artist(self, name):
            return [{"id": "artist-2", "name": "Deft Ones Band", "external_urls": {}}]

        def top_tracks(self, artist_id, market):
            return [
                {
                    "id": "track-2",
                    "name": track.title,
                    "duration_ms": 240000,
                    "artists": [{"name": "Deft Ones Band"}],
                    "album": {"name": track.album.title},
                    "external_urls": {},
                }
            ]

    monkeypatch.setattr("enrichment.services.SpotifyClient", FakeSpotify)
    enrich_spotify(artist)
    evidence = NoteworthyEvidence.objects.get(external_track__source_record__external_id="track-2")
    assert evidence.decision == Decision.PENDING


@pytest.mark.django_db
def test_recommendations_rank_absent_artists_by_distinct_library_links(artist):
    mastodon = Artist.objects.create(name="Mastodon", normalized_name="mastodon")
    team_sleep = Artist.objects.create(name="Team Sleep", normalized_name="team sleep")
    relationships = [
        (artist, "Failure", Source.LASTFM),
        (artist, "Failure", Source.WIKIPEDIA),
        (mastodon, "Failure", Source.MUSICBRAINZ),
        (artist, "Hum", Source.LASTFM),
        (artist, "Team Sleep", Source.WIKIPEDIA),
    ]
    for index, (source_artist, related_name, source) in enumerate(relationships):
        RelatedArtistEvidence.objects.create(
            artist=source_artist,
            related_artist_name=related_name,
            relationship_type=RelatedArtistEvidence.RelationshipType.SIMILAR
            if source == Source.LASTFM
            else RelatedArtistEvidence.RelationshipType.COLLABORATOR,
            source=source,
            confidence=Decimal("0.8"),
            decision=Decision.PENDING,
        )

    assert refresh_artist_recommendations() == {
        "recommendations": 2,
        "top_artist": "Failure",
    }
    failure, hum = ArtistRecommendation.objects.all()
    assert (failure.name, failure.linked_artist_count, failure.evidence_count) == (
        "Failure",
        2,
        3,
    )
    assert failure.linked_artists == ["Deftones", "Mastodon"]
    assert (hum.name, hum.rank) == ("Hum", 2)
    local_relationship = RelatedArtistEvidence.objects.get(related_artist_name="Team Sleep")
    assert local_relationship.related_artist == team_sleep


@pytest.mark.django_db
def test_recommendations_exclude_featured_artist_credits(artist):
    RelatedArtistEvidence.objects.create(
        artist=artist,
        related_artist_name="Failure feat. Hayley Williams & Chino Moreno",
        relationship_type=RelatedArtistEvidence.RelationshipType.COLLABORATOR,
        source=Source.WIKIPEDIA,
        confidence=Decimal("0.8"),
        decision=Decision.PENDING,
    )
    RelatedArtistEvidence.objects.create(
        artist=artist,
        related_artist_name="Hum",
        relationship_type=RelatedArtistEvidence.RelationshipType.SIMILAR,
        source=Source.LASTFM,
        confidence=Decimal("0.8"),
        decision=Decision.PENDING,
    )

    assert refresh_artist_recommendations()["recommendations"] == 1
    assert list(ArtistRecommendation.objects.values_list("name", flat=True)) == ["Hum"]


@pytest.mark.django_db
def test_featured_song_title_can_match_and_be_noteworthy(track, artist, monkeypatch):
    from enrichment.services import enrich_lastfm

    track.title = "Passenger (feat. Maynard James Keenan)"
    track.normalized_title = normalize_text(track.title)
    track.save(update_fields=["title", "normalized_title"])

    class FakeLastFm:
        def artist_top_tracks(self, name, limit):
            return [{"name": "Passenger", "playcount": "5000", "url": ""}]

        def similar_artists(self, name, limit=30):
            return []

    monkeypatch.setattr("enrichment.services.LastFmClient", FakeLastFm)
    enrich_lastfm(artist)

    evidence = NoteworthyEvidence.objects.get(
        evidence_type=NoteworthyEvidence.EvidenceType.LASTFM_TOP
    )
    assert evidence.track == track
    assert evidence.decision == Decision.ACCEPTED


@pytest.mark.django_db
def test_lastfm_reenrichment_preserves_manual_review_decision(track, artist, monkeypatch):
    from enrichment.services import enrich_lastfm

    class FakeLastFm:
        def artist_top_tracks(self, name, limit):
            return [{"name": track.title, "playcount": "5000", "url": ""}]

        def similar_artists(self, name, limit=30):
            return []

    monkeypatch.setattr("enrichment.services.LastFmClient", FakeLastFm)
    enrich_lastfm(artist)
    evidence = NoteworthyEvidence.objects.get(
        evidence_type=NoteworthyEvidence.EvidenceType.LASTFM_TOP
    )
    evidence.decision = Decision.REJECTED
    evidence.decision_is_manual = True
    evidence.save(update_fields=["decision", "decision_is_manual", "updated_at"])

    enrich_lastfm(artist)

    evidence.refresh_from_db()
    assert evidence.decision == Decision.REJECTED
    assert evidence.decision_is_manual is True


@pytest.mark.django_db
def test_api_client_retries_transient_tls_failures(monkeypatch):
    client = BaseClient()

    class SuccessResponse:
        ok = True

        def json(self):
            return {"ok": True}

    responses = iter([requests.exceptions.SSLError("temporary EOF"), SuccessResponse()])

    def request(*args, **kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(client.session, "request", request)
    monkeypatch.setattr("enrichment.clients.time.sleep", lambda seconds: None)

    assert client.json("GET", "https://example.test") == {"ok": True}


@pytest.mark.django_db
def test_api_client_opens_shared_cooldown_for_youtube_daily_quota(monkeypatch):
    class FakeRedis:
        values = {}

        def get(self, name):
            return self.values.get(name)

        def set(self, name, value, ex):
            self.values[name] = value

    class QuotaResponse:
        ok = False
        status_code = 429
        headers = {}
        text = "Quota exceeded for quota metric 'Search Queries per day'"

    calls = []
    BaseClient.cooldown_redis = FakeRedis()
    BaseClient.local_cooldowns.clear()
    client = BaseClient()
    client.provider_name = "youtube"
    monkeypatch.setattr(
        client.session,
        "request",
        lambda *args, **kwargs: calls.append(args) or QuotaResponse(),
    )

    with pytest.raises(RateLimited) as first_error:
        client.json("GET", "https://youtube.test/search")
    assert first_error.value.retry_after == 24 * 60 * 60
    assert len(calls) == 1

    second_client = BaseClient()
    second_client.provider_name = "youtube"
    monkeypatch.setattr(
        second_client.session,
        "request",
        lambda *args, **kwargs: pytest.fail("cooldown should prevent another API request"),
    )
    with pytest.raises(RateLimited):
        second_client.json("GET", "https://youtube.test/search")

    BaseClient.cooldown_redis = None
    BaseClient.local_cooldowns.clear()


@pytest.mark.django_db
def test_musicbrainz_rate_limit_uses_shared_redis_lock(monkeypatch):
    events = []

    class FakeLock:
        def acquire(self, blocking=True):
            events.append(("acquire", blocking))
            return True

        def release(self):
            events.append(("release",))

    class FakeRedis:
        def lock(self, name, timeout, blocking_timeout):
            events.append(("lock", name, timeout, blocking_timeout))
            return FakeLock()

        def get(self, name):
            events.append(("get", name))
            return "0"

        def set(self, name, value, ex):
            events.append(("set", name, ex))

    monkeypatch.setattr(MusicBrainzClient, "rate_redis", FakeRedis())
    monkeypatch.setattr(BaseClient, "json", lambda self, method, url, **kwargs: {"ok": True})

    client = MusicBrainzClient()
    assert client.json("GET", "https://musicbrainz.test") == {"ok": True}
    assert events[0] == (
        "lock",
        "kuratorr:musicbrainz:request-lock",
        180,
        240,
    )
    assert ("acquire", True) in events
    assert events[-1] == ("release",)


@pytest.mark.django_db
def test_missing_provider_configuration_is_a_skip_not_a_job_error(artist, monkeypatch):
    from enrichment.tasks import ENRICHERS, enrich_artist

    def missing_provider(current_artist):
        raise ProviderNotConfigured("API key is not configured in Settings")

    monkeypatch.setitem(ENRICHERS, "lastfm", missing_provider)
    result = enrich_artist(artist, "lastfm")

    assert result == {"lastfm": {"skipped": "API key is not configured in Settings"}}
    assert artist.source_statuses.get(source="lastfm").last_error == ""


@pytest.mark.django_db
def test_pending_enrichment_retries_stale_musicbrainz_failures(artist, monkeypatch):
    from enrichment.models import ArtistSourceStatus
    from enrichment.tasks import enrich_artist_task, run_pending_enrichments

    status = ArtistSourceStatus.objects.create(
        artist=artist,
        source=Source.MUSICBRAINZ,
        last_attempted_at=timezone.now() - timedelta(minutes=20),
        last_error="temporary TLS failure",
    )
    for source in (Source.SPOTIFY, Source.LASTFM, Source.WIKIPEDIA, Source.YOUTUBE):
        ArtistSourceStatus.objects.create(artist=artist, source=source)
    queued = []
    monkeypatch.setattr(
        enrich_artist_task,
        "delay",
        lambda artist_id, source=None, job_id=None: queued.append((artist_id, source)),
    )

    assert run_pending_enrichments() == 1
    assert queued == [(artist.pk, Source.MUSICBRAINZ)]
    status.refresh_from_db()
    assert status.retry_at > timezone.now() + timedelta(hours=23)

    status.last_attempted_at = timezone.now()
    status.save(update_fields=["last_attempted_at"])
    queued.clear()
    assert run_pending_enrichments() == 0
    assert queued == []


@pytest.mark.django_db
def test_pending_scheduler_does_not_start_unrequested_artist_enrichment(artist, monkeypatch):
    from enrichment.tasks import enrich_artist_task, run_pending_enrichments

    monkeypatch.setattr(
        enrich_artist_task,
        "delay",
        lambda *args, **kwargs: pytest.fail("unrequested enrichment was queued"),
    )

    assert run_pending_enrichments() == 0


@pytest.mark.django_db
def test_rate_limited_source_is_deferred_until_retry_time(artist, monkeypatch):
    from enrichment.models import ArtistSourceStatus
    from enrichment.tasks import (
        ENRICHERS,
        enrich_artist,
        enrich_artist_task,
        run_pending_enrichments,
    )

    def quota_exhausted(current_artist):
        raise RateLimited("youtube", 24 * 60 * 60, "YouTube daily quota exhausted")

    monkeypatch.setitem(ENRICHERS, "youtube", quota_exhausted)
    result = enrich_artist(artist, "youtube")
    status = artist.source_statuses.get(source=Source.YOUTUBE)

    assert result["youtube"]["rate_limited"] is True
    assert result["youtube"]["retry_after_seconds"] == 24 * 60 * 60
    assert status.consecutive_failures == 1
    assert status.retry_at > timezone.now() + timedelta(hours=23)

    for source in (Source.MUSICBRAINZ, Source.SPOTIFY, Source.LASTFM, Source.WIKIPEDIA):
        ArtistSourceStatus.objects.create(artist=artist, source=source)

    queued = []
    monkeypatch.setattr(
        enrich_artist_task,
        "delay",
        lambda artist_id, source=None, job_id=None: queued.append((artist_id, source)),
    )
    assert run_pending_enrichments() == 0
    assert queued == []

    status.retry_at = timezone.now() - timedelta(seconds=1)
    status.save(update_fields=["retry_at"])
    assert run_pending_enrichments() == 1
    assert queued == [(artist.pk, Source.YOUTUBE)]


@pytest.mark.django_db
def test_provider_error_is_deferred_instead_of_failing_artist_job(artist, monkeypatch):
    from enrichment.models import JobRun
    from enrichment.tasks import ENRICHERS, enrich_artist_task

    def quota_exhausted(current_artist):
        raise RateLimited("youtube", 24 * 60 * 60, "YouTube daily quota exhausted")

    monkeypatch.setitem(ENRICHERS, "youtube", quota_exhausted)
    job = JobRun.objects.create(job_type="enrich_youtube", requested_manually=True)

    result = enrich_artist_task(artist.pk, "youtube", job.pk)

    job.refresh_from_db()
    assert job.status == JobRun.Status.SUCCEEDED
    assert job.error == ""
    assert result["deferred_sources"] == ["youtube"]
    assert job.summary["youtube"]["rate_limited"] is True


@pytest.mark.django_db
def test_retry_scheduler_skips_provider_during_shared_cooldown(artist, monkeypatch):
    from enrichment.models import ArtistSourceStatus
    from enrichment.tasks import BaseClient, enrich_artist_task, run_pending_enrichments

    ArtistSourceStatus.objects.create(
        artist=artist,
        source=Source.YOUTUBE,
        last_attempted_at=timezone.now() - timedelta(days=1),
        last_error="daily quota exhausted",
        retry_at=timezone.now() - timedelta(seconds=1),
    )
    monkeypatch.setattr(
        BaseClient,
        "cooldown_remaining_for",
        classmethod(lambda cls, source: 3600 if source == Source.YOUTUBE else 0),
    )
    monkeypatch.setattr(
        enrich_artist_task,
        "delay",
        lambda *args, **kwargs: pytest.fail("cooling-down provider was queued"),
    )

    assert run_pending_enrichments() == 0


@pytest.mark.django_db
def test_library_enrichment_children_advance_parent_progress(track, monkeypatch):
    from enrichment.models import JobRun
    from enrichment.tasks import (
        ENRICHERS,
        enrich_artist_task,
        enrich_library_task,
        refresh_artist_recommendations_task,
    )
    from playlists.tasks import generate_playlists_task

    for source in ENRICHERS:
        monkeypatch.setitem(ENRICHERS, source, lambda current_artist: {"processed": 1})
    monkeypatch.setattr(refresh_artist_recommendations_task, "delay", lambda: None)
    monkeypatch.setattr(generate_playlists_task, "delay", lambda: None)

    def run_child(artist_id, source=None, job_id=None):
        enrich_artist_task(artist_id, source, job_id)
        return type("Result", (), {"id": f"child-{job_id}"})()

    monkeypatch.setattr(enrich_artist_task, "delay", run_child)
    parent = JobRun.objects.create(job_type="enrich_library", requested_manually=True)

    result = enrich_library_task(parent.pk)

    parent.refresh_from_db()
    child = parent.child_jobs.get()
    assert result == {"queued": 1}
    assert parent.status == JobRun.Status.SUCCEEDED
    assert (parent.progress_current, parent.progress_total) == (1, 1)
    assert child.status == JobRun.Status.SUCCEEDED


@pytest.mark.django_db
def test_deferred_provider_does_not_fail_library_enrichment(track, monkeypatch):
    from enrichment.models import JobRun
    from enrichment.tasks import (
        ENRICHERS,
        enrich_artist_task,
        enrich_library_task,
        refresh_artist_recommendations_task,
    )
    from playlists.tasks import generate_playlists_task

    def quota_exhausted(current_artist):
        raise RateLimited("youtube", 24 * 60 * 60, "YouTube daily quota exhausted")

    for source in ENRICHERS:
        monkeypatch.setitem(ENRICHERS, source, lambda current_artist: {"processed": 1})
    monkeypatch.setitem(ENRICHERS, "youtube", quota_exhausted)
    monkeypatch.setattr(refresh_artist_recommendations_task, "delay", lambda: None)
    monkeypatch.setattr(generate_playlists_task, "delay", lambda: None)

    def run_child(artist_id, source=None, job_id=None):
        enrich_artist_task(artist_id, source, job_id)
        return type("Result", (), {"id": f"child-{job_id}"})()

    monkeypatch.setattr(enrich_artist_task, "delay", run_child)
    parent = JobRun.objects.create(job_type="enrich_library", requested_manually=True)

    enrich_library_task(parent.pk)

    parent.refresh_from_db()
    assert parent.status == JobRun.Status.SUCCEEDED
    assert parent.summary["failed"] == 0
    assert parent.summary["artists_with_deferred_sources"] == 1


def test_manual_and_control_tasks_have_higher_priority():
    from enrichment.tasks import (
        CONTROL_PRIORITY,
        enrich_artist_task,
        enrich_library_task,
        refresh_artist_recommendations_task,
        refresh_noteworthy_decisions_task,
        run_pending_enrichments,
    )
    from library.tasks import scan_root_task
    from playlists.tasks import generate_playlists_task, materialize_playlists_task

    assert enrich_artist_task.priority < CONTROL_PRIORITY
    for task in (
        enrich_library_task,
        refresh_artist_recommendations_task,
        refresh_noteworthy_decisions_task,
        run_pending_enrichments,
        scan_root_task,
        generate_playlists_task,
        materialize_playlists_task,
    ):
        assert task.priority == CONTROL_PRIORITY


def test_celery_tasks_are_routed_to_independent_queues(settings):
    routes = settings.CELERY_TASK_ROUTES

    assert routes["enrichment.tasks.enrich_artist_task"]["queue"] == "enrichment"
    assert (
        routes["enrichment.tasks.refresh_artist_recommendations_task"]["queue"] == "recommendations"
    )
    assert routes["playlists.tasks.*"]["queue"] == "playlists"
    assert routes["library.tasks.*"]["queue"] == "control"
