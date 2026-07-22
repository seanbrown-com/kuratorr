from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from dashboard.sorting import apply_sorting
from enrichment.models import (
    ArtistRecommendation,
    Decision,
    JobRun,
    MissingAlbum,
    NoteworthyEvidence,
)
from enrichment.services import missing_albums_with_notable_tracks
from enrichment.tasks import ENRICHERS, enrich_artist_task
from library.models import Artist


@login_required
def recommendations(request):
    recommendations, sorting = apply_sorting(
        request,
        ArtistRecommendation.objects.all(),
        {"rank": "rank", "recommended": Lower("name")},
        "rank",
    )
    page = Paginator(recommendations, 100).get_page(request.GET.get("page"))
    last_job = JobRun.objects.filter(job_type="refresh_recommendations").first()
    return render(
        request,
        "enrichment/recommendations.html",
        {"page": page, "last_job": last_job, **sorting},
    )


@login_required
def missing_albums(request):
    albums = MissingAlbum.objects.select_related("artist", "source_record")
    query = request.GET.get("q", "").strip()
    release_type = request.GET.get("release_type", "").strip()
    release_types = list(
        MissingAlbum.objects.exclude(release_type="")
        .order_by("release_type")
        .values_list("release_type", flat=True)
        .distinct()
    )
    if query:
        albums = albums.filter(Q(artist__name__icontains=query) | Q(title__icontains=query))
    if release_type in release_types:
        albums = albums.filter(release_type=release_type)
    else:
        release_type = ""
    albums, sorting = apply_sorting(
        request,
        albums,
        {
            "artist": Lower("artist__name"),
            "album": Lower("title"),
            "year": "year",
            "release_type": Lower("release_type"),
        },
        "artist",
    )
    page = Paginator(missing_albums_with_notable_tracks(albums), 100).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "enrichment/missing_albums.html",
        {
            "page": page,
            "query": query,
            "release_types": release_types,
            "selected_release_type": release_type,
            **sorting,
        },
    )


@login_required
def review_queue(request):
    selected = request.GET.get("decision", Decision.PENDING)
    evidence = NoteworthyEvidence.objects.filter(decision=selected).select_related(
        "artist",
        "track",
        "track__album",
        "external_track",
        "external_track__source_record",
    )
    evidence, sorting = apply_sorting(
        request,
        evidence,
        {
            "source": "external_track__source_record__source",
            "artist": Lower("artist__name"),
            "external_title": Lower("external_track__title"),
            "local_match": Lower("track__title"),
            "confidence": "confidence",
        },
        "source",
    )
    page = Paginator(evidence, 100).get_page(request.GET.get("page"))
    return render(
        request,
        "enrichment/review_queue.html",
        {
            "page": page,
            "selected": selected,
            "decisions": Decision.choices,
            **sorting,
        },
    )


@require_POST
@login_required
def review_evidence(request, pk, decision):
    if decision not in Decision.values:
        return redirect("review-queue")
    evidence = get_object_or_404(NoteworthyEvidence, pk=pk)
    evidence.decision = decision
    evidence.save(update_fields=["decision", "updated_at"])
    if evidence.external_track:
        evidence.external_track.match_decision = decision
        evidence.external_track.save(update_fields=["match_decision", "updated_at"])
    messages.success(request, "Review decision saved.")
    return redirect(request.POST.get("next") or "review-queue")


@require_POST
@login_required
def run_artist_source(request, artist_id, source):
    artist = get_object_or_404(Artist, pk=artist_id)
    if source not in ENRICHERS:
        messages.error(request, "Unknown enrichment source.")
        return redirect("artist-detail", pk=artist.pk)
    job = JobRun.objects.create(job_type=f"enrich_{source}", requested_manually=True)
    result = enrich_artist_task.delay(artist.pk, source, job.pk)
    job.celery_task_id = result.id
    job.save(update_fields=["celery_task_id", "updated_at"])
    messages.success(request, f"{source.title()} enrichment queued for {artist.name}.")
    return redirect("artist-detail", pk=artist.pk)
