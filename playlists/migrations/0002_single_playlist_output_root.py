from django.db import migrations


def keep_one_output_root(apps, schema_editor):
    output_root = apps.get_model("playlists", "PlaylistOutputRoot")
    first = output_root.objects.order_by("pk").first()
    if not first:
        return
    path = first.path
    enabled = first.enabled
    output_root.objects.all().delete()
    output_root.objects.create(pk=1, path=path, enabled=enabled)


class Migration(migrations.Migration):
    dependencies = [
        ("playlists", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(keep_one_output_root, migrations.RunPython.noop),
    ]
