import hmac

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_celery_results.models import TaskResult

from dashboard.forms import InitialSetupForm, ServiceSettingsForm
from enrichment.job_control import cancel_job, reconcile_stale_jobs, replace_active_job
from enrichment.models import (
    ArtistSourceStatus,
    Decision,
    JobRun,
    NoteworthyEvidence,
)
from enrichment.tasks import (
    enrich_library_task,
    refresh_artist_recommendations_task,
    refresh_noteworthy_decisions_task,
)
from library.models import Album, Artist, LibraryRoot, ServiceSettings, Track
from playlists.forms import PlaylistOutputRootForm
from playlists.models import Playlist, PlaylistOutputRoot
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
    reconcile_stale_jobs()
    context = {
        "artist_count": Artist.objects.filter(tracks__is_available=True).distinct().count(),
        "album_count": Album.objects.count(),
        "track_count": Track.objects.filter(is_available=True).count(),
        "playlist_count": Playlist.objects.filter(deleted_at=None).count(),
        "pending_reviews": NoteworthyEvidence.objects.filter(decision=Decision.PENDING).count(),
        "roots": LibraryRoot.objects.all(),
        "jobs": JobRun.objects.all()[:12],
    }
    return render(request, "dashboard/index.html", context)


def _filtered_jobs(params):
    jobs = JobRun.objects.all()
    selected_status = params.get("status", "")
    selected_type = params.get("type", "")
    selected_requested = params.get("requested", "")
    if selected_status in JobRun.Status.values:
        jobs = jobs.filter(status=selected_status)
    else:
        selected_status = ""
    if selected_type:
        jobs = jobs.filter(job_type=selected_type)
    if selected_requested == "manual":
        jobs = jobs.filter(requested_manually=True)
    elif selected_requested == "automatic":
        jobs = jobs.filter(requested_manually=False)
    else:
        selected_requested = ""
    return jobs, selected_status, selected_type, selected_requested


def _clearable_job_ids(params):
    """Find filtered terminal jobs without crossing an active job hierarchy."""
    jobs, selected_status, selected_type, selected_requested = _filtered_jobs(params)
    terminal_statuses = [
        JobRun.Status.SUCCEEDED,
        JobRun.Status.FAILED,
        JobRun.Status.CANCELLED,
    ]
    candidate_ids = set(jobs.filter(status__in=terminal_statuses).values_list("pk", flat=True))

    # Completed children are still needed while an active parent aggregates its
    # progress. Protect every descendant of an active job from history cleanup.
    frontier = set(
        JobRun.objects.filter(status__in=[JobRun.Status.QUEUED, JobRun.Status.RUNNING]).values_list(
            "pk", flat=True
        )
    )
    while frontier:
        descendants = set(
            JobRun.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
        )
        new_descendants = descendants & candidate_ids
        candidate_ids.difference_update(new_descendants)
        frontier = descendants

    # Deleting a parent cascades to its children. If any child falls outside the
    # filtered cleanup set, retain the parent so no unselected history disappears.
    while candidate_ids:
        parents_with_retained_children = set(
            JobRun.objects.filter(parent_id__in=candidate_ids)
            .exclude(pk__in=candidate_ids)
            .values_list("parent_id", flat=True)
        )
        if not parents_with_retained_children:
            break
        candidate_ids.difference_update(parents_with_retained_children)

    return (
        candidate_ids,
        selected_status,
        selected_type,
        selected_requested,
    )


@login_required
def job_history(request):
    reconcile_stale_jobs()
    jobs, selected_status, selected_type, selected_requested = _filtered_jobs(request.GET)
    job_types = JobRun.objects.order_by("job_type").values_list("job_type", flat=True).distinct()
    page = Paginator(jobs, 100).get_page(request.GET.get("page"))
    return render(
        request,
        "dashboard/job_history.html",
        {
            "page": page,
            "statuses": JobRun.Status.choices,
            "job_types": job_types,
            "selected_status": selected_status,
            "selected_type": selected_type,
            "selected_requested": selected_requested,
        },
    )


@login_required
def clear_job_history(request):
    params = request.POST if request.method == "POST" else request.GET
    clearable_ids, selected_status, selected_type, selected_requested = _clearable_job_ids(params)
    if request.method == "POST" and request.POST.get("confirm") == "yes":
        with transaction.atomic():
            clearable_ids, _, _, _ = _clearable_job_ids(request.POST)
            task_ids = list(
                JobRun.objects.filter(pk__in=clearable_ids)
                .exclude(celery_task_id="")
                .values_list("celery_task_id", flat=True)
            )
            _, deleted_by_model = JobRun.objects.filter(pk__in=clearable_ids).delete()
            if task_ids:
                TaskResult.objects.filter(task_id__in=task_ids).delete()
        deleted_jobs = deleted_by_model.get("enrichment.JobRun", 0)
        messages.success(request, f"Removed {deleted_jobs} past job(s) from history.")
        return redirect("job-history")
    if request.method == "POST":
        messages.error(request, "Confirm that you want to permanently clear this job history.")
    return render(
        request,
        "dashboard/job_history_confirm_clear.html",
        {
            "clearable_count": len(clearable_ids),
            "selected_status": selected_status,
            "selected_type": selected_type,
            "selected_requested": selected_requested,
        },
    )


@login_required
def settings_view(request):
    instance = ServiceSettings.load()
    # Newly created singleton defaults can retain Python floats until reloaded;
    # normalize them to the database field types before comparing form values.
    instance.refresh_from_db()
    decision_fields = {
        "spotify_noteworthy_max_rank",
        "lastfm_min_playcount",
        "lastfm_noteworthy_max_rank",
        "track_match_review_threshold",
        "track_match_auto_accept_threshold",
    }
    original_decision_values = {field: getattr(instance, field) for field in decision_fields}
    action = request.POST.get("action", "save_service_settings")
    output_root = PlaylistOutputRoot.objects.order_by("pk").first()
    settings_data = (
        request.POST if request.method == "POST" and action != "save_playlist_output" else None
    )
    output_data = (
        request.POST if request.method == "POST" and action == "save_playlist_output" else None
    )
    form = ServiceSettingsForm(settings_data, instance=instance)
    output_form = PlaylistOutputRootForm(output_data, instance=output_root)
    if request.method == "POST" and action == "save_playlist_output":
        if output_form.is_valid():
            saved_root = output_form.save()
            PlaylistOutputRoot.objects.exclude(pk=saved_root.pk).delete()
            messages.success(request, "Playlist output directory saved.")
            return redirect("settings")
    elif request.method == "POST" and form.is_valid():
        saved_settings = form.save()
        if form.updated_sources:
            ArtistSourceStatus.objects.filter(source__in=form.updated_sources).delete()
        if any(
            form.cleaned_data[field] != original_decision_values[field] for field in decision_fields
        ):
            ServiceSettings.objects.filter(pk=saved_settings.pk).update(
                noteworthy_decision_revision=F("noteworthy_decision_revision") + 1
            )
            saved_settings.refresh_from_db(fields=["noteworthy_decision_revision"])
            job = replace_active_job(
                "refresh_noteworthy_decisions",
                requested_manually=True,
            )
            result = refresh_noteworthy_decisions_task.delay(
                job_id=job.pk,
                settings_revision=saved_settings.noteworthy_decision_revision,
            )
            job.celery_task_id = result.id
            job.save(update_fields=["celery_task_id", "updated_at"])
        messages.success(request, "Settings saved.")
        return redirect("settings")
    return render(
        request,
        "dashboard/settings.html",
        {
            "form": form,
            "output_form": output_form,
            "playlist_output_root": output_root,
        },
    )


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
    if JobRun.objects.filter(
        job_type=job_type, status__in=[JobRun.Status.QUEUED, JobRun.Status.RUNNING]
    ).exists():
        messages.warning(request, f"{job_type.replace('_', ' ').title()} is already active.")
        return redirect("dashboard")
    job = JobRun.objects.create(job_type=job_type, requested_manually=True)
    result = callbacks[job_type].delay(job_id=job.pk)
    job.celery_task_id = result.id
    job.save(update_fields=["celery_task_id", "updated_at"])
    messages.success(request, f"{job_type.replace('_', ' ').title()} queued.")
    return redirect("dashboard")


@require_POST
@login_required
def cancel_job_view(request, pk):
    job = get_object_or_404(JobRun, pk=pk)
    if job.status not in [JobRun.Status.QUEUED, JobRun.Status.RUNNING]:
        messages.warning(request, "Only queued or running jobs can be cancelled.")
    else:
        cancelled = cancel_job(job)
        messages.success(request, f"Cancelled {cancelled} queued or running job(s).")
    return redirect(request.POST.get("next") or "job-history")
