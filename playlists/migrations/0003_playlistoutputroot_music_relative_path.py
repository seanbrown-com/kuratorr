from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("playlists", "0002_single_playlist_output_root"),
    ]

    operations = [
        migrations.AddField(
            model_name="playlistoutputroot",
            name="music_relative_path",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Relative path from the output directory to the music library as seen by "
                    "the device reading the M3U files. For example: ../.."
                ),
                max_length=2048,
                verbose_name="M3U music path relative to output directory",
            ),
        ),
    ]
