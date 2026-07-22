from celery import shared_task

from enrichment.job_control import JobCancelled, finish_job, start_job, touch_job
from enrichment.models import JobRun
from playlists.services import (
    generate_artist_playlists,
    generate_grouped_playlists,
    generate_radio_playlists,
    materialize_all,
)


def _run(job_type, callback, job_id=None, task_id=""):
    job = JobRun.objects.get(pk=job_id) if job_id else JobRun.objects.create(job_type=job_type)
    try:
        start_job(job, task_id)
        result = callback()
        touch_job(job.pk)
        summary = {"processed": result}
        finish_job(job, JobRun.Status.SUCCEEDED, summary=summary)
        return summary
    except JobCancelled:
        return {"cancelled": True}
    except Exception as exc:
        finish_job(job, JobRun.Status.FAILED, error=str(exc))
        raise


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
