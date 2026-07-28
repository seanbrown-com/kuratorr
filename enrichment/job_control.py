from datetime import timedelta

from celery import current_app
from django.db import IntegrityError, transaction
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


def touch_job(job_id, *, current=None, total=None, current_item=None):
    values = {"heartbeat_at": timezone.now(), "updated_at": timezone.now()}
    if current is not None:
        values["progress_current"] = current
    if total is not None:
        values["progress_total"] = total
    if current_item is not None:
        values["current_item"] = current_item
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
    if status == JobRun.Status.SUCCEEDED:
        job.current_item = ""
    job.save()


def fail_job_for_task(task_id, error):
    """Record failures raised by Celery outside the task process itself."""
    if not task_id:
        return 0
    now = timezone.now()
    job = JobRun.objects.filter(
        celery_task_id=task_id,
        status__in=[JobRun.Status.QUEUED, JobRun.Status.RUNNING],
    ).first()
    if not job:
        return 0
    JobRun.objects.filter(pk=job.pk).update(
        status=JobRun.Status.FAILED,
        finished_at=now,
        heartbeat_at=now,
        error=str(error)[:10000],
        updated_at=now,
    )
    if job.parent_id:
        update_parent_from_children(job.parent_id)
    return 1


def reconcile_stale_jobs(max_silence=timedelta(minutes=15)):
    now = timezone.now()
    stale_before = now - max_silence
    stale = JobRun.objects.filter(status=JobRun.Status.RUNNING).filter(
        Q(finished_at__isnull=False)
        | Q(heartbeat_at__lt=stale_before)
        | Q(heartbeat_at__isnull=True, started_at__lt=stale_before)
    )
    count = 0
    parent_ids = set()
    for job in stale.iterator():
        job.status = JobRun.Status.FAILED
        job.finished_at = job.finished_at or now
        job.heartbeat_at = now
        job.error = job.error or "Job stopped without completing; no worker heartbeat was received."
        job.save(update_fields=["status", "finished_at", "heartbeat_at", "error", "updated_at"])
        if job.parent_id:
            parent_ids.add(job.parent_id)
        count += 1
    for parent_id in parent_ids:
        update_parent_from_children(parent_id)
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


def replace_active_job(job_type, *, requested_manually=False):
    """Cancel the active job of this type and atomically create its replacement."""
    for attempt in range(3):
        revoked_task_ids = []
        try:
            with transaction.atomic():
                active = list(
                    JobRun.objects.select_for_update().filter(
                        job_type=job_type,
                        status__in=[JobRun.Status.QUEUED, JobRun.Status.RUNNING],
                    )
                )
                now = timezone.now()
                for item in active:
                    item.status = JobRun.Status.CANCELLED
                    item.finished_at = now
                    item.heartbeat_at = now
                    item.error = "Superseded by newer settings."
                    item.save(
                        update_fields=[
                            "status",
                            "finished_at",
                            "heartbeat_at",
                            "error",
                            "updated_at",
                        ]
                    )
                    if item.celery_task_id:
                        revoked_task_ids.append(item.celery_task_id)
                job = JobRun.objects.create(
                    job_type=job_type,
                    requested_manually=requested_manually,
                )
            for task_id in revoked_task_ids:
                current_app.control.revoke(task_id, terminate=False)
            return job
        except IntegrityError:
            if attempt == 2:
                raise
    raise RuntimeError(f"Could not replace active {job_type} job")


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
        deferred = sum(
            bool((child.summary or {}).get("deferred_sources"))
            for child in terminal.only("summary")
        )
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
                "artists_with_deferred_sources": deferred,
            }
            if failed or cancelled:
                parent.error = (
                    f"{failed} artist enrichment job(s) failed; {cancelled} were cancelled."
                )
            finished_successfully = parent.status == JobRun.Status.SUCCEEDED
        parent.save()
        return finished_successfully
