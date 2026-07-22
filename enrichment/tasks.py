from datetime import timedelta

from celery import shared_task
from django.db.models import Count, Q
from django.utils import timezone

from enrichment.clients import ProviderNotConfigured, RateLimited
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


def enrich_artist(artist, source=None):
    callbacks = {source: ENRICHERS[source]} if source else ENRICHERS
    result = {}
    for name, callback in callbacks.items():
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
    job.status = JobRun.Status.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id or ""
    job.save()
    try:
        job.summary = enrich_artist(Artist.objects.get(pk=artist_id), source)
        errors = [value for value in job.summary.values() if "error" in value]
        job.status = (
            JobRun.Status.FAILED if len(errors) == len(job.summary) else JobRun.Status.SUCCEEDED
        )
        if errors:
            job.error = "; ".join(x["error"] for x in errors)
    except Exception as exc:
        job.status = JobRun.Status.FAILED
        job.error = str(exc)
        raise
    finally:
        job.finished_at = timezone.now()
        job.save()
    return job.summary


@shared_task(bind=True)
def enrich_library_task(self, job_id=None):
    job = (
        JobRun.objects.get(pk=job_id)
        if job_id
        else JobRun.objects.create(job_type="enrich_library")
    )
    artists = Artist.objects.all()
    job.status = JobRun.Status.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id or ""
    job.progress_total = artists.count()
    job.save()
    results = {}
    try:
        for index, artist in enumerate(artists.iterator(), 1):
            results[str(artist.pk)] = enrich_artist(artist)
            job.progress_current = index
            job.save(update_fields=["progress_current", "updated_at"])
        job.summary = {"artists": len(results), "results": results}
        job.status = JobRun.Status.SUCCEEDED
        from playlists.tasks import generate_playlists_task

        refresh_artist_recommendations_task.delay()
        generate_playlists_task.delay()
    except Exception as exc:
        job.status = JobRun.Status.FAILED
        job.error = str(exc)
        raise
    finally:
        job.finished_at = timezone.now()
        job.save()
    return job.summary


@shared_task
def run_pending_enrichments():
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

    pending = Artist.objects.annotate(source_count=Count("source_statuses")).filter(
        source_count__lt=len(ENRICHERS)
    )[:5]
    for artist in pending:
        enrich_artist_task.delay(artist.pk)
    return len(pending)


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
