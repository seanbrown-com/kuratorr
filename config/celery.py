import os

from celery import Celery
from celery.signals import task_failure, task_revoked

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("kuratorr")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@task_failure.connect
def record_task_failure(task_id=None, exception=None, **kwargs):
    from enrichment.job_control import fail_job_for_task

    fail_job_for_task(task_id, f"Celery task failed: {exception}")


@task_revoked.connect
def record_terminated_task(request=None, terminated=False, expired=False, signum=None, **kwargs):
    from enrichment.job_control import fail_job_for_task

    if request is not None and (terminated or expired):
        reason = (
            f"Celery terminated the task (signal {signum})."
            if terminated
            else "Celery task expired."
        )
        fail_job_for_task(request.id, reason)
