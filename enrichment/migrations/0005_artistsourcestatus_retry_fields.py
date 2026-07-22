from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("enrichment", "0004_missingalbum"),
    ]

    operations = [
        migrations.AddField(
            model_name="artistsourcestatus",
            name="consecutive_failures",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="artistsourcestatus",
            name="retry_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
