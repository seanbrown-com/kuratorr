from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("enrichment", "0009_primary_artist_credits")]

    operations = [
        migrations.AlterField(
            model_name="noteworthyevidence",
            name="evidence_type",
            field=models.CharField(
                choices=[
                    ("spotify_top", "Spotify top track"),
                    ("lastfm_top", "Last.fm top track"),
                    ("musicbrainz_single", "MusicBrainz single"),
                    ("wikipedia_single", "Wikipedia single"),
                    ("wikipedia_video", "Wikipedia music video"),
                    ("youtube_official", "YouTube official music video"),
                    ("manual", "Manual"),
                ],
                max_length=40,
            ),
        )
    ]
