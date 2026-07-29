from django.db import migrations, models


def set_track_minimum(apps, schema_editor):
    service_settings = apps.get_model("library", "ServiceSettings")
    service_settings.objects.update(minimum_playlist_tracks=25)


def restore_duration_minimum(apps, schema_editor):
    service_settings = apps.get_model("library", "ServiceSettings")
    service_settings.objects.update(minimum_playlist_tracks=3600)


class Migration(migrations.Migration):
    dependencies = [("library", "0007_servicesettings_noteworthy_decision_revision")]

    operations = [
        migrations.RenameField(
            model_name="servicesettings",
            old_name="minimum_playlist_seconds",
            new_name="minimum_playlist_tracks",
        ),
        migrations.AlterField(
            model_name="servicesettings",
            name="minimum_playlist_tracks",
            field=models.PositiveSmallIntegerField(
                default=25,
                help_text=(
                    "Minimum number of distinct tracks required for any generated playlist."
                ),
            ),
        ),
        migrations.RunPython(set_track_minimum, restore_duration_minimum),
    ]
