from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("library", "0005_remove_track_raw_metadata")]

    operations = [
        migrations.AlterField(
            model_name="servicesettings",
            name="track_match_auto_accept_threshold",
            field=models.DecimalField(
                decimal_places=3,
                default=0.9,
                help_text="Minimum whole-title similarity accepted automatically.",
                max_digits=4,
            ),
        ),
        migrations.RemoveField(
            model_name="servicesettings",
            name="youtube_auto_accept_confidence",
        ),
    ]
