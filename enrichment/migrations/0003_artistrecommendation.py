from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("enrichment", "0002_artistsourcestatus")]

    operations = [
        migrations.CreateModel(
            name="ArtistRecommendation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=500)),
                ("normalized_name", models.CharField(max_length=500, unique=True)),
                ("rank", models.PositiveIntegerField(db_index=True)),
                ("linked_artist_count", models.PositiveIntegerField(default=0)),
                ("evidence_count", models.PositiveIntegerField(default=0)),
                ("linked_artists", models.JSONField(default=list)),
                ("sources", models.JSONField(default=list)),
                ("relationship_types", models.JSONField(default=list)),
            ],
            options={"ordering": ["rank", "name"]},
        )
    ]
