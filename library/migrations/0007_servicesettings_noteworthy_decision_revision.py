from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("library", "0006_lower_track_auto_accept_threshold")]

    operations = [
        migrations.AddField(
            model_name="servicesettings",
            name="noteworthy_decision_revision",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
    ]
