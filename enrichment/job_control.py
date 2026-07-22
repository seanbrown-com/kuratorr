from datetime import timedelta

from celery import current_app
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from enrichment.models import JobRun


class JobCancelled(RuntimeError):
    pass


def start_job(job, task_id=""):
    job.refresh_from_db()
    if job.status == JobRun.Status.CANCELLED:
        raise JobCancelled("Job was cancelled before it started.")
    now = timezone.now()
    job.status = JobRun.Status.RUNNING
    job.started_at = job.started_at or now
    job.heartbeat_at = now
    job.finished_at = None
    job.celery_task_id = task_id or job.celery_task_id
    job.save()


def touch_job(job_id, *, current=None, total=None):
    values = {"heartbeat_at": timezone.now(), "updated_at": timezone.now()}
    if current is not None:
        values["progress_current"] = current
    if total is not None:
        values["progress_total"] = total
    updated = JobRun.objects.filter(pk=job_id, status=JobRun.Status.RUNNING).update(**values)
    if not updated:
        raise JobCancelled("Job is no longer running.")


def finish_job(job, status, *, summary=None, error=""):
    job.refresh_from_db()
    if job.status == JobRun.Status.CANCELLED:
        return
    now = timezone.now()
    job.status = status
    job.finished_at = now
    job.heartbeat_at = now
    if summary is not None:
        job.summary = summary
    job.error = error
    job.save()


def reconcile_stale_jobs(max_silence=timedelta(hours=1)):
    now = timezone.now()
    stale_before = now - max_silence
    stale = JobRun.objects.filter(status=JobRun.Status.RUNNING).filter(
        Q(finished_at__isnull=False)
        | Q(heartbeat_at__lt=stale_before)
        | Q(heartbeat_at__isnull=True, started_at__lt=stale_before)
    )
    count = 0
    for job in stale.iterator():
        job.status = JobRun.Status.FAILED
        job.finished_at = job.finished_at or now
        job.heartbeat_at = now
        job.error = job.error or "Job stopped without completing; no worker heartbeat was received."
        job.save(update_fields=["status", "finished_at", "heartbeat_at", "error", "updated_at"])
        count += 1
    return count


def cancel_job(job):
    now = timezone.now()
    jobs = list(
        JobRun.objects.filter(Q(pk=job.pk) | Q(parent=job)).filter(
            status__in=[JobRun.Status.QUEUED, JobRun.Status.RUNNING]
        )
    )
    for item in jobs:
        item.status = JobRun.Status.CANCELLED
        item.finished_at = now
        item.heartbeat_at = now
        item.error = "Cancelled by the administrator."
        item.save(update_fields=["status", "finished_at", "heartbeat_at", "error", "updated_at"])
        if item.celery_task_id:
            current_app.control.revoke(item.celery_task_id, terminate=False)
    return len(jobs)


def update_parent_from_children(parent_id):
    if not parent_id:
        return False
    with transaction.atomic():
        parent = JobRun.objects.select_for_update().get(pk=parent_id)
        if parent.status != JobRun.Status.RUNNING:
            return False
        terminal = parent.child_jobs.filter(
            status__in=[
                JobRun.Status.SUCCEEDED,
                JobRun.Status.FAILED,
                JobRun.Status.CANCELLED,
            ]
        )
        completed = terminal.count()
        failed = terminal.filter(status=JobRun.Status.FAILED).count()
        cancelled = terminal.filter(status=JobRun.Status.CANCELLED).count()
        now = timezone.now()
        parent.progress_current = completed
        parent.heartbeat_at = now
        finished_successfully = False
        if completed >= parent.progress_total:
            parent.status = (
                JobRun.Status.SUCCEEDED if not failed and not cancelled else JobRun.Status.FAILED
            )
            parent.finished_at = now
            parent.summary = {
                "artists": parent.progress_total,
                "succeeded": completed - failed - cancelled,
                "failed": failed,
                "cancelled": cancelled,
            }
            if failed or cancelled:
                parent.error = (
                    f"{failed} artist enrichment job(s) failed; {cancelled} were cancelled."
                )
            finished_successfully = parent.status == JobRun.Status.SUCCEEDED
        parent.save()
        return finished_successfully
