from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("library", "0004_servicesettings_track_match_auto_accept_threshold_and_more")]

    operations = [
        migrations.RemoveField(
            model_name="track",
            name="raw_metadata",
        ),
    ]
