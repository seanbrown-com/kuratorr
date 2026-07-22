from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from enrichment.clients import ProviderNotConfigured, RateLimited
from enrichment.job_control import (
    JobCancelled,
    finish_job,
    reconcile_stale_jobs,
    start_job,
    touch_job,
    update_parent_from_children,
)
from enrichment.models import ArtistSourceStatus, JobRun
from enrichment.services import (
    enrich_lastfm,
    enrich_musicbrainz,
    enrich_spotify,
    enrich_wikipedia,
    enrich_youtube,
    refresh_album_genres,
    refresh_artist_recommendations,
    refresh_noteworthy_decisions,
)
from library.models import Artist

ENRICHERS = {
    "musicbrainz": enrich_musicbrainz,
    "spotify": enrich_spotify,
    "lastfm": enrich_lastfm,
    "wikipedia": enrich_wikipedia,
    "youtube": enrich_youtube,
}


def enrich_artist(artist, source=None, cancellation_check=None):
    callbacks = {source: ENRICHERS[source]} if source else ENRICHERS
    result = {}
    for name, callback in callbacks.items():
        if cancellation_check:
            cancellation_check()
        status, _ = ArtistSourceStatus.objects.get_or_create(artist=artist, source=name)
        status.last_attempted_at = timezone.now()
        try:
            result[name] = callback(artist)
            status.last_succeeded_at = timezone.now()
            status.last_error = ""
            status.retry_at = None
            status.consecutive_failures = 0
        except ProviderNotConfigured as exc:
            result[name] = {"skipped": str(exc)}
            status.last_error = ""
            status.retry_at = None
            status.consecutive_failures = 0
        except RateLimited as exc:
            status.consecutive_failures += 1
            exponential_delay = min(60 * (2 ** (status.consecutive_failures - 1)), 6 * 60 * 60)
            retry_after = max(exc.retry_after, exponential_delay)
            status.retry_at = timezone.now() + timedelta(seconds=retry_after)
            status.last_error = str(exc)
            result[name] = {
                "error": str(exc),
                "rate_limited": True,
                "retry_after_seconds": retry_after,
            }
        except Exception as exc:
            status.consecutive_failures += 1
            retry_after = min(15 * 60 * (2 ** (status.consecutive_failures - 1)), 24 * 60 * 60)
            status.retry_at = timezone.now() + timedelta(seconds=retry_after)
            result[name] = {"error": str(exc)}
            status.last_error = str(exc)
        status.save()
    refresh_album_genres()
    refresh_noteworthy_decisions(artist)
    return result


@shared_task(bind=True)
def enrich_artist_task(self, artist_id, source=None, job_id=None):
    job = (
        JobRun.objects.get(pk=job_id)
        if job_id
        else JobRun.objects.create(job_type=f"enrich_{source or 'artist'}")
    )
    try:
        start_job(job, self.request.id or "")
        summary = enrich_artist(
            Artist.objects.get(pk=artist_id),
            source,
            cancellation_check=lambda: touch_job(job.pk),
        )
        errors = [value for value in summary.values() if "error" in value]
        status = JobRun.Status.FAILED if errors else JobRun.Status.SUCCEEDED
        error = "; ".join(x["error"] for x in errors)
        finish_job(job, status, summary=summary, error=error)
        return summary
    except JobCancelled:
        return {"cancelled": True}
    except Exception as exc:
        finish_job(job, JobRun.Status.FAILED, error=str(exc))
        raise
    finally:
        if job.parent_id and update_parent_from_children(job.parent_id):
            from playlists.tasks import generate_playlists_task

            refresh_artist_recommendations_task.delay()
            generate_playlists_task.delay()


@shared_task(bind=True)
def enrich_library_task(self, job_id=None):
    job = (
        JobRun.objects.get(pk=job_id)
        if job_id
        else JobRun.objects.create(job_type="enrich_library")
    )
    try:
        start_job(job, self.request.id or "")
        artists = Artist.objects.filter(tracks__is_available=True).distinct()
        total = artists.count()
        touch_job(job.pk, current=0, total=total)
        if not total:
            finish_job(job, JobRun.Status.SUCCEEDED, summary={"artists": 0})
            return {"queued": 0}
        for artist in artists.iterator():
            touch_job(job.pk)
            child = JobRun.objects.create(
                job_type="enrich_artist",
                requested_manually=False,
                parent=job,
            )
            result = enrich_artist_task.delay(artist.pk, job_id=child.pk)
            child.celery_task_id = result.id
            child.save(update_fields=["celery_task_id", "updated_at"])
        return {"queued": total}
    except JobCancelled:
        return {"cancelled": True}
    except Exception as exc:
        finish_job(job, JobRun.Status.FAILED, error=str(exc))
        raise


@shared_task
def run_pending_enrichments():
    reconcile_stale_jobs()
    now = timezone.now()
    retry_before = timezone.now() - timedelta(minutes=15)
    failed_sources = list(
        ArtistSourceStatus.objects.exclude(last_error="")
        .filter(
            Q(retry_at__lte=now)
            | Q(retry_at__isnull=True, last_attempted_at__isnull=True)
            | Q(retry_at__isnull=True, last_attempted_at__lt=retry_before)
        )
        .select_related("artist")
        .order_by("retry_at", "last_attempted_at")[:5]
    )
    if failed_sources:
        lease_until = now + timedelta(minutes=15)
        for status in failed_sources:
            status.retry_at = lease_until
            status.save(update_fields=["retry_at", "updated_at"])
            enrich_artist_task.delay(status.artist_id, status.source)
        return len(failed_sources)

    return 0


@shared_task(bind=True)
def refresh_noteworthy_decisions_task(self, job_id=None):
    job = (
        JobRun.objects.get(pk=job_id)
        if job_id
        else JobRun.objects.create(job_type="refresh_noteworthy_decisions")
    )
    try:
        start_job(job, self.request.id or "")
        summary = refresh_noteworthy_decisions()
        finish_job(job, JobRun.Status.SUCCEEDED, summary=summary)
        return summary
    except JobCancelled:
        return {"cancelled": True}
    except Exception as exc:
        finish_job(job, JobRun.Status.FAILED, error=str(exc))
        raise


@shared_task(bind=True)
def refresh_artist_recommendations_task(self, job_id=None):
    job = (
        JobRun.objects.get(pk=job_id)
        if job_id
        else JobRun.objects.create(job_type="refresh_recommendations")
    )
    job.status = JobRun.Status.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id or ""
    job.save()
    try:
        job.summary = refresh_artist_recommendations()
        job.progress_current = job.summary["recommendations"]
        job.progress_total = job.summary["recommendations"]
        job.status = JobRun.Status.SUCCEEDED
    except Exception as exc:
        job.status = JobRun.Status.FAILED
        job.error = str(exc)
        raise
    finally:
        job.finished_at = timezone.now()
        job.save()
    return job.summary
