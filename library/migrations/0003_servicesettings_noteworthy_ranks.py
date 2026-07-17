from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("library", "0002_servicesettings_credentials")]

    operations = [
        migrations.AddField(
            model_name="servicesettings",
            name="spotify_noteworthy_max_rank",
            field=models.PositiveSmallIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="servicesettings",
            name="lastfm_noteworthy_max_rank",
            field=models.PositiveSmallIntegerField(default=2),
        ),
    ]
