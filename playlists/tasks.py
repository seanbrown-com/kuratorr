from celery import shared_task
from django.utils import timezone

from enrichment.models import JobRun
from playlists.services import (
    generate_artist_playlists,
    generate_grouped_playlists,
    generate_radio_playlists,
    materialize_all,
)


def _run(job_type, callback, job_id=None, task_id=""):
    job = JobRun.objects.get(pk=job_id) if job_id else JobRun.objects.create(job_type=job_type)
    job.status = JobRun.Status.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = task_id
    job.save()
    try:
        result = callback()
        job.summary = {"processed": result}
        job.status = JobRun.Status.SUCCEEDED
        return job.summary
    except Exception as exc:
        job.status = JobRun.Status.FAILED
        job.error = str(exc)
        raise
    finally:
        job.finished_at = timezone.now()
        job.save()


@shared_task(bind=True)
def generate_playlists_task(self, job_id=None):
    return _run(
        "generate_playlists",
        lambda: (
            generate_artist_playlists() + generate_grouped_playlists() + generate_radio_playlists()
        ),
        job_id,
        self.request.id or "",
    )


@shared_task(bind=True)
def materialize_playlists_task(self, job_id=None):
    return _run("materialize_playlists", materialize_all, job_id, self.request.id or "")
