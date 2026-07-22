from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("enrichment", "0006_jobrun_heartbeat_at_jobrun_parent")]

    operations = [
        migrations.AddField(
            model_name="jobrun",
            name="current_item",
            field=models.CharField(blank=True, max_length=4096),
        ),
    ]
