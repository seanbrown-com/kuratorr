import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("enrichment", "0005_artistsourcestatus_retry_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobrun",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="jobrun",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="child_jobs",
                to="enrichment.jobrun",
            ),
        ),
    ]
