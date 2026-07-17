from celery import shared_task
from django.utils import timezone

from enrichment.models import JobRun
from library.models import LibraryRoot
from library.services import scan_library_root


@shared_task(bind=True)
def scan_root_task(self, root_id, job_id=None):
    job = (
        JobRun.objects.get(pk=job_id) if job_id else JobRun.objects.create(job_type="scan_library")
    )
    job.status = JobRun.Status.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id or ""
    job.save()
    try:
        job.summary = scan_library_root(LibraryRoot.objects.get(pk=root_id))
        job.status = JobRun.Status.SUCCEEDED
        from enrichment.tasks import enrich_library_task

        enrich_library_task.delay()
    except Exception as exc:
        job.status = JobRun.Status.FAILED
        job.error = str(exc)
        raise
    finally:
        job.finished_at = timezone.now()
        job.save()
    return job.summary
