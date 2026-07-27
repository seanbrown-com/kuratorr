import re
import unicodedata

from django.db import migrations


FEATURE_CREDIT_RE = re.compile(
    r"(?:\s+|[\(\[])(?:feat(?:uring)?|ft)\.?\s+",
    flags=re.IGNORECASE,
)


def primary_artist_name(value):
    value = (value or "").strip()
    match = FEATURE_CREDIT_RE.search(value)
    if not match:
        return value
    return value[: match.start()].rstrip(" ,;:-([")


def normalize_text(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def consolidate_primary_artist_credits(apps, schema_editor):
    Artist = apps.get_model("library", "Artist")
    Album = apps.get_model("library", "Album")
    Track = apps.get_model("library", "Track")
    ArtistRecommendation = apps.get_model("enrichment", "ArtistRecommendation")

    collaborative_artists = [
        artist
        for artist in Artist.objects.all().iterator()
        if FEATURE_CREDIT_RE.search(artist.name)
    ]
    for collaborative in collaborative_artists:
        primary_name = primary_artist_name(collaborative.name)
        normalized = normalize_text(primary_name)
        if not normalized or normalized == collaborative.normalized_name:
            continue
        primary, _ = Artist.objects.get_or_create(
            normalized_name=normalized,
            defaults={"name": primary_name, "sort_name": primary_name},
        )
        Track.objects.filter(artist_id=collaborative.pk).update(artist_id=primary.pk)
        for album in Album.objects.filter(artist_id=collaborative.pk).iterator():
            target = (
                Album.objects.filter(
                    artist_id=primary.pk,
                    normalized_title=album.normalized_title,
                    year=album.year,
                )
                .exclude(pk=album.pk)
                .first()
            )
            if target:
                Track.objects.filter(album_id=album.pk).update(album_id=target.pk)
            else:
                album.artist_id = primary.pk
                album.save(update_fields=["artist"])

    recommendation_ids = [
        recommendation.pk
        for recommendation in ArtistRecommendation.objects.all().iterator()
        if FEATURE_CREDIT_RE.search(recommendation.name)
    ]
    ArtistRecommendation.objects.filter(pk__in=recommendation_ids).delete()


class Migration(migrations.Migration):
    dependencies = [("enrichment", "0008_reclassify_deferred_enrichment_jobs")]

    operations = [
        migrations.RunPython(
            consolidate_primary_artist_credits,
            migrations.RunPython.noop,
        )
    ]
