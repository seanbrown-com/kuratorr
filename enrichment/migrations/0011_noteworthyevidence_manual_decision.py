from django.db import migrations, models
from django.utils import timezone


def cancel_duplicate_refresh_jobs(apps, schema_editor):
    job_run = apps.get_model("enrichment", "JobRun")
    active = job_run.objects.filter(
        job_type="refresh_noteworthy_decisions",
        status__in=["queued", "running"],
    ).order_by("-created_at")
    keep = active.values_list("pk", flat=True).first()
    if keep:
        active.exclude(pk=keep).update(
            status="cancelled",
            finished_at=timezone.now(),
            heartbeat_at=timezone.now(),
            error="Superseded while installing single-job reconciliation.",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("enrichment", "0010_musicbrainz_single_evidence"),
        ("library", "0006_lower_track_auto_accept_threshold"),
    ]

    operations = [
        migrations.AddField(
            model_name="noteworthyevidence",
            name="decision_is_manual",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(cancel_duplicate_refresh_jobs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="jobrun",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    job_type="refresh_noteworthy_decisions",
                    status__in=["queued", "running"],
                ),
                fields=("job_type",),
                name="one_active_noteworthy_refresh",
            ),
        ),
    ]
