import os
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from enrichment.job_control import reconcile_stale_jobs
from enrichment.models import JobRun
from library.forms import LibraryRootForm
from library.models import Artist, LibraryRoot, Track
from library.tasks import scan_root_task


def _directory_browser(requested_path):
    """Return a safe, display-only directory listing from the service filesystem."""
    default_path = next(
        (path for path in (Path("/libraries"), Path("/music")) if path.is_dir()), Path("/")
    )
    try:
        current = Path(requested_path or default_path).expanduser().resolve(strict=True)
        if not current.is_dir():
            raise NotADirectoryError
        with os.scandir(current) as entries:
            directories = sorted(
                (
                    {"name": entry.name, "path": str(Path(entry.path).resolve())}
                    for entry in entries
                    if entry.is_dir(follow_symlinks=True)
                ),
                key=lambda entry: entry["name"].casefold(),
            )
        return {
            "current": str(current),
            "parent": str(current.parent) if current != current.parent else None,
            "directories": directories,
            "error": None,
        }
    except (OSError, RuntimeError):
        fallback = default_path.resolve()
        return {
            "current": str(fallback),
            "parent": str(fallback.parent) if fallback != fallback.parent else None,
            "directories": [],
            "error": f"The directory {requested_path!s} cannot be browsed by the service.",
        }


@login_required
def track_list(request):
    query = request.GET.get("q", "").strip()
    tracks = Track.objects.filter(is_available=True).select_related("artist", "album")
    if query:
        tracks = tracks.filter(
            Q(title__icontains=query)
            | Q(artist__name__icontains=query)
            | Q(album__title__icontains=query)
        )
    page = Paginator(tracks, 100).get_page(request.GET.get("page"))
    return render(request, "library/track_list.html", {"page": page, "query": query})


@login_required
def artist_list(request):
    query = request.GET.get("q", "").strip()
    artists = (
        Artist.objects.filter(tracks__is_available=True)
        .annotate(
            album_count=Count("albums", distinct=True),
            track_count=Count("tracks", distinct=True),
        )
        .distinct()
    )
    if query:
        artists = artists.filter(name__icontains=query)
    page = Paginator(artists, 100).get_page(request.GET.get("page"))
    return render(request, "library/artist_list.html", {"page": page, "query": query})


@login_required
def artist_detail(request, pk):
    artist = get_object_or_404(Artist, pk=pk)
    from playlists.services import noteworthy_tracks

    return render(
        request,
        "library/artist_detail.html",
        {"artist": artist, "greatest_hits": noteworthy_tracks(artist)},
    )


@login_required
def root_list(request):
    form = LibraryRootForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Library root added.")
        return redirect("root-list")
    browser = _directory_browser(request.GET.get("browse"))
    if request.method == "GET":
        form = LibraryRootForm(initial={"path": browser["current"], "enabled": True})
    return render(
        request,
        "library/root_list.html",
        {"roots": LibraryRoot.objects.all(), "form": form, "browser": browser},
    )


@require_POST
@login_required
def scan_root(request, pk):
    root = get_object_or_404(LibraryRoot, pk=pk)
    reconcile_stale_jobs()
    if JobRun.objects.filter(
        job_type="scan_library", status__in=[JobRun.Status.QUEUED, JobRun.Status.RUNNING]
    ).exists():
        messages.warning(request, "A library scan is already queued or running.")
        return redirect("root-list")
    job = JobRun.objects.create(job_type="scan_library", requested_manually=True)
    result = scan_root_task.delay(root.pk, job.pk)
    job.celery_task_id = result.id
    job.save(update_fields=["celery_task_id", "updated_at"])
    messages.success(request, f"Scan queued for {root.path}.")
    return redirect("root-list")
