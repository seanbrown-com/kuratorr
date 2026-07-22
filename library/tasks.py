from celery import shared_task

from enrichment.job_control import JobCancelled, finish_job, start_job, touch_job
from enrichment.models import JobRun
from library.models import LibraryRoot
from library.services import scan_library_root


@shared_task(bind=True)
def scan_root_task(self, root_id, job_id=None):
    job = (
        JobRun.objects.get(pk=job_id) if job_id else JobRun.objects.create(job_type="scan_library")
    )
    try:
        start_job(job, self.request.id or "")
        summary = scan_library_root(
            LibraryRoot.objects.get(pk=root_id),
            progress_callback=lambda current, total: touch_job(
                job.pk, current=current, total=total
            ),
        )
        finish_job(job, JobRun.Status.SUCCEEDED, summary=summary)
        return summary
    except JobCancelled:
        return {"cancelled": True}
    except Exception as exc:
        finish_job(job, JobRun.Status.FAILED, error=str(exc))
        raise
