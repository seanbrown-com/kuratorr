from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from playlists.forms import PlaylistExportForm, PlaylistOutputRootForm
from playlists.models import Playlist, PlaylistOutputRoot
from playlists.services import (
    _safe_filename,
    delete_playlist,
    render_copy_script,
    render_m3u,
    render_m3u_zip,
    restore_playlist,
)


@login_required
def playlist_list(request):
    playlist_type = request.GET.get("type", "")
    playlists = Playlist.objects.filter(deleted_at=None)
    if playlist_type:
        playlists = playlists.filter(playlist_type=playlist_type)
    return render(
        request,
        "playlists/list.html",
        {
            "playlists": playlists,
            "selected_type": playlist_type,
            "types": Playlist.PlaylistType.choices,
            "export_form": PlaylistExportForm(),
        },
    )


@login_required
def deleted_list(request):
    return render(
        request, "playlists/deleted.html", {"playlists": Playlist.objects.exclude(deleted_at=None)}
    )


@login_required
def playlist_detail(request, pk):
    playlist = get_object_or_404(Playlist, pk=pk)
    return render(
        request,
        "playlists/detail.html",
        {"playlist": playlist, "export_form": PlaylistExportForm()},
    )


@require_POST
@login_required
def download_m3u(request, pk):
    playlist = get_object_or_404(Playlist, pk=pk, deleted_at=None)
    form = PlaylistExportForm(request.POST)
    if not form.is_valid():
        return render(request, "playlists/detail.html", {"playlist": playlist, "export_form": form})
    response = HttpResponse(
        render_m3u(playlist, form.cleaned_data["source_directory"]),
        content_type="audio/x-mpegurl",
    )
    response["Content-Disposition"] = f'attachment; filename="{_safe_filename(playlist.name)}.m3u"'
    return response


@require_POST
@login_required
def download_all_m3u(request):
    form = PlaylistExportForm(request.POST)
    playlists = Playlist.objects.filter(deleted_at=None)
    if not form.is_valid():
        return render(
            request,
            "playlists/list.html",
            {
                "playlists": playlists,
                "selected_type": "",
                "types": Playlist.PlaylistType.choices,
                "export_form": form,
            },
        )
    response = HttpResponse(
        render_m3u_zip(playlists, form.cleaned_data["source_directory"]),
        content_type="application/zip",
    )
    response["Content-Disposition"] = 'attachment; filename="playlists.zip"'
    return response


@login_required
def download_copy_script(request, pk):
    playlist = get_object_or_404(Playlist, pk=pk, deleted_at=None)
    response = HttpResponse(render_copy_script(playlist), content_type="text/x-shellscript")
    response["Content-Disposition"] = (
        f'attachment; filename="copy-{_safe_filename(playlist.name)}.sh"'
    )
    return response


@require_POST
@login_required
def delete_view(request, pk):
    playlist = get_object_or_404(Playlist, pk=pk)
    delete_playlist(playlist, permanent=request.POST.get("permanent") == "on")
    messages.success(request, "Playlist deleted. It can be restored from Deleted Playlists.")
    return redirect("playlist-list")


@require_POST
@login_required
def restore_view(request, pk):
    playlist = get_object_or_404(Playlist, pk=pk)
    restore_playlist(playlist)
    messages.success(request, "Playlist restored and eligible for regeneration.")
    return redirect("deleted-playlists")


@login_required
def output_roots(request):
    form = PlaylistOutputRootForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Playlist output directory added.")
        return redirect("playlist-output-roots")
    return render(
        request, "playlists/outputs.html", {"roots": PlaylistOutputRoot.objects.all(), "form": form}
    )
