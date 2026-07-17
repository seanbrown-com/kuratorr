from datetime import timedelta
from decimal import Decimal

import pytest
import requests
from django.utils import timezone

from enrichment.clients import BaseClient, MusicBrainzClient, ProviderNotConfigured
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
    _match_local_track,
    _section_candidates,
    _wikipedia_infobox,
    _youtube_confidence,
    _youtube_title,
    enrich_spotify,
    enrich_musicbrainz,
    enrich_wikipedia,
    refresh_artist_recommendations,
    refresh_noteworthy_decisions,
)
from library.models import Album, Artist, Track
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
    matched, confidence, decision = _match_local_track(
        artist, "Change in the House of Flys"
    )

    assert matched == track
    assert Decimal("0.85") <= confidence < Decimal("0.95")
    assert decision == Decision.PENDING


def test_wikipedia_parser_reads_single_and_video_sections():
    html = """
    <h2>Singles</h2><table><tr><th>Year</th><th>Title</th></tr>
    <tr><td>2000</td><td>\"Change\"</td></tr></table>
    <h2>Music videos</h2><ul><li>2001 – \"Back to School\"</li></ul>
    """
    values = _section_candidates(html)
    assert ("wikipedia_single", "Change", 2000) in values
    assert ("wikipedia_video", "Back to School", 2001) in values


def test_wikipedia_parser_removes_reference_markup_and_unmatched_quote():
    html = """
    <h2>Music videos</h2><table>
      <tr><th>Year</th><th>Title</th></tr>
      <tr><td>2005</td><td>Image of the Invisible" <sup class="reference"><a href="./cite_note-33">[ 33 ]</a></sup></td></tr>
      <tr><td>2008</td><td>"Come All You Weary"<sup class="reference"><a href="./cite_note-36">[36]</a></sup></td></tr>
    </table>
    """

    assert _section_candidates(html) == [
        ("wikipedia_video", "Image of the Invisible", 2005),
        ("wikipedia_video", "Come All You Weary", 2008),
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
    assert ("wikipedia_single", "Album Single", 2025) in values
    assert ("wikipedia_single", "Standalone Single", 2025) in values
    assert all(title != "Non-album single" for _, title, _ in values)


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
def test_musicbrainz_catalog_records_only_missing_albums(artist, album, monkeypatch):
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
                    "title": "Minerva",
                    "primary-type": "Single",
                    "first-release-date": "2003",
                },
            ]

        def relationships(self, artist_mbid):
            return []

    monkeypatch.setattr("enrichment.services.MusicBrainzClient", FakeMusicBrainz)

    assert enrich_musicbrainz(artist)["albums"] == 1
    missing = MissingAlbum.objects.get()
    assert (missing.title, missing.year, missing.release_type) == (
        "Saturday Night Wrist",
        2006,
        "Album",
    )


def test_youtube_requires_explicit_official_music_video_and_artist_channel():
    artist = type("Artist", (), {"name": "Team Sleep"})()
    plain_upload = {
        "snippet": {"title": "Team Sleep - Blvd. Nights", "description": "", "channelTitle": "Team Sleep"}
    }
    official = {
        "snippet": {"title": "Team Sleep - Blvd. Nights (Official Music Video)", "description": "", "channelTitle": "Team Sleep"}
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

    for rank, title in enumerate(["No One Loves Me", "New Fang", "Dead End Friends", "Scumbag Blues"], 1):
        evidence(Source.SPOTIFY, NoteworthyEvidence.EvidenceType.SPOTIFY_TOP, title, rank=rank)
    for rank, title in enumerate(["New Fang", "Dead End Friends", "Scumbag Blues", "No One Loves Me"], 1):
        evidence(Source.LASTFM, NoteworthyEvidence.EvidenceType.LASTFM_TOP, title, rank=rank, playcount=10000)
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

    status.last_attempted_at = timezone.now()
    status.save(update_fields=["last_attempted_at"])
    queued.clear()
    assert run_pending_enrichments() == 0
    assert queued == []
