import hmac

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from dashboard.forms import InitialSetupForm, ServiceSettingsForm
from enrichment.models import (
    ArtistRecommendation,
    ArtistSourceStatus,
    Decision,
    JobRun,
    NoteworthyEvidence,
)
from enrichment.tasks import enrich_library_task, refresh_artist_recommendations_task
from enrichment.services import refresh_noteworthy_decisions
from library.models import Album, Artist, LibraryRoot, ServiceSettings, Track
from playlists.models import Playlist
from playlists.tasks import generate_playlists_task, materialize_playlists_task


def initial_setup(request):
    User = get_user_model()
    if User.objects.exists():
        return redirect("login")
    if not settings.INITIAL_SETUP_TOKEN:
        return HttpResponseForbidden("INITIAL_SETUP_TOKEN is not configured on the server.")
    form = InitialSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not hmac.compare_digest(form.cleaned_data["token"], settings.INITIAL_SETUP_TOKEN):
            form.add_error("token", "Invalid setup token.")
        else:
            user = User.objects.create_superuser(
                form.cleaned_data["username"], password=form.cleaned_data["password1"]
            )
            login(request, user)
            return redirect("dashboard")
    return render(request, "dashboard/setup.html", {"form": form})


def health(request):
    return HttpResponse("ok", content_type="text/plain")


@login_required
def dashboard(request):
    context = {
        "artist_count": Artist.objects.count(),
        "album_count": Album.objects.count(),
        "track_count": Track.objects.filter(is_available=True).count(),
        "playlist_count": Playlist.objects.filter(deleted_at=None).count(),
        "pending_reviews": NoteworthyEvidence.objects.filter(decision=Decision.PENDING).count(),
        "recommendation_count": ArtistRecommendation.objects.count(),
        "roots": LibraryRoot.objects.all(),
        "jobs": JobRun.objects.all()[:12],
    }
    return render(request, "dashboard/index.html", context)


@login_required
def settings_view(request):
    instance = ServiceSettings.load()
    form = ServiceSettingsForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        refresh_noteworthy_decisions()
        if form.updated_sources:
            ArtistSourceStatus.objects.filter(source__in=form.updated_sources).delete()
        messages.success(request, "Settings saved.")
        return redirect("settings")
    return render(request, "dashboard/settings.html", {"form": form})


@require_POST
@login_required
def run_job(request, job_type):
    callbacks = {
        "enrich_library": enrich_library_task,
        "generate_playlists": generate_playlists_task,
        "materialize_playlists": materialize_playlists_task,
        "refresh_recommendations": refresh_artist_recommendations_task,
    }
    if job_type not in callbacks:
        return HttpResponse("Unknown job", status=404)
    job = JobRun.objects.create(job_type=job_type, requested_manually=True)
    result = callbacks[job_type].delay(job_id=job.pk)
    job.celery_task_id = result.id
    job.save(update_fields=["celery_task_id", "updated_at"])
    messages.success(request, f"{job_type.replace('_', ' ').title()} queued.")
    return redirect("dashboard")
