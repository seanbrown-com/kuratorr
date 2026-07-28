import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("enrichment", "0011_noteworthyevidence_manual_decision"),
        ("library", "0007_servicesettings_noteworthy_decision_revision"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoteworthyDecisionStage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("run_id", models.UUIDField(db_index=True)),
                ("external_title", models.CharField(blank=True, max_length=1000)),
                (
                    "confidence",
                    models.DecimalField(decimal_places=3, default=0, max_digits=4),
                ),
                (
                    "decision",
                    models.CharField(
                        choices=[
                            ("pending", "Pending review"),
                            ("accepted", "Accepted"),
                            ("rejected", "Rejected"),
                        ],
                        max_length=20,
                    ),
                ),
                ("notes", models.TextField(blank=True)),
                (
                    "evidence",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="enrichment.noteworthyevidence",
                    ),
                ),
                (
                    "external_track",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="enrichment.externaltrack",
                    ),
                ),
                (
                    "matched_track",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="library.track",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="noteworthydecisionstage",
            constraint=models.UniqueConstraint(
                fields=("run_id", "evidence"),
                name="unique_noteworthy_stage_evidence",
            ),
        ),
    ]
