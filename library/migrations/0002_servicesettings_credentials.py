from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("library", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="servicesettings",
            name="spotify_client_id_encrypted",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="servicesettings",
            name="spotify_client_secret_encrypted",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="servicesettings",
            name="lastfm_api_key_encrypted",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="servicesettings",
            name="youtube_api_key_encrypted",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="servicesettings",
            name="http_user_agent",
            field=models.CharField(
                blank=True,
                help_text="Identify this service with a contact email or URL for MusicBrainz/Wikimedia.",
                max_length=500,
            ),
        ),
    ]
