from django.db import migrations
from django.utils import timezone


def reclassify_deferred_jobs(apps, schema_editor):
    JobRun = apps.get_model("enrichment", "JobRun")
    parent_ids = set()
    children = JobRun.objects.filter(job_type="enrich_artist", status="failed").exclude(summary={})
    for child in children.iterator():
        deferred_sources = [
            name
            for name, value in (child.summary or {}).items()
            if isinstance(value, dict) and "error" in value
        ]
        if not deferred_sources:
            continue
        child.summary["deferred_sources"] = deferred_sources
        child.status = "succeeded"
        child.error = ""
        child.save(update_fields=["summary", "status", "error", "updated_at"])
        if child.parent_id:
            parent_ids.add(child.parent_id)

    now = timezone.now()
    for parent in JobRun.objects.filter(pk__in=parent_ids):
        children = JobRun.objects.filter(parent_id=parent.pk)
        terminal = children.filter(status__in=["succeeded", "failed", "cancelled"])
        completed = terminal.count()
        failed = terminal.filter(status="failed").count()
        cancelled = terminal.filter(status="cancelled").count()
        deferred = sum(
            bool((child.summary or {}).get("deferred_sources"))
            for child in terminal.only("summary")
        )
        parent.progress_current = completed
        parent.heartbeat_at = now
        parent.summary = {
            "artists": parent.progress_total,
            "succeeded": completed - failed - cancelled,
            "failed": failed,
            "cancelled": cancelled,
            "artists_with_deferred_sources": deferred,
        }
        if completed >= parent.progress_total:
            parent.status = "succeeded" if not failed and not cancelled else "failed"
            parent.finished_at = parent.finished_at or now
            parent.error = (
                f"{failed} artist enrichment job(s) failed; {cancelled} were cancelled."
                if failed or cancelled
                else ""
            )
        parent.save()


class Migration(migrations.Migration):
    dependencies = [("enrichment", "0007_jobrun_current_item")]

    operations = [migrations.RunPython(reclassify_deferred_jobs, migrations.RunPython.noop)]
