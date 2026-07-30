import re
import unicodedata

from django.db import migrations, models


def normalize_text(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = re.sub(r"\([^)]*(remaster|version|edit|mix)[^)]*\)", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def populate_normalized_album_titles(apps, schema_editor):
    external_track = apps.get_model("enrichment", "ExternalTrack")
    batch = []
    tracks = (
        external_track.objects.exclude(album_title="")
        .only("pk", "album_title", "normalized_album_title")
        .iterator(chunk_size=2000)
    )
    for track in tracks:
        track.normalized_album_title = normalize_text(track.album_title)
        batch.append(track)
        if len(batch) == 2000:
            external_track.objects.bulk_update(batch, ["normalized_album_title"])
            batch.clear()
    if batch:
        external_track.objects.bulk_update(batch, ["normalized_album_title"])


class Migration(migrations.Migration):
    dependencies = [
        ("enrichment", "0012_noteworthydecisionstage"),
    ]

    operations = [
        migrations.AddField(
            model_name="externaltrack",
            name="normalized_album_title",
            field=models.CharField(blank=True, default="", max_length=700),
            preserve_default=False,
        ),
        migrations.RunPython(
            populate_normalized_album_titles,
            migrations.RunPython.noop,
        ),
        migrations.AddIndex(
            model_name="externaltrack",
            index=models.Index(
                fields=["artist", "normalized_album_title"],
                name="external_artist_album_idx",
            ),
        ),
    ]
