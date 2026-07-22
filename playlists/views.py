from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from dashboard.sorting import apply_sorting
from playlists.models import Playlist
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
    playlists, sorting = apply_sorting(
        request,
        playlists,
        {
            "name": "name",
            "type": "playlist_type",
            "tracks": "track_count",
            "duration": "duration_seconds",
        },
        "name",
    )
    return render(
        request,
        "playlists/list.html",
        {
            "playlists": playlists,
            "selected_type": playlist_type,
            "types": Playlist.PlaylistType.choices,
            **sorting,
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
        {"playlist": playlist},
    )


@login_required
def download_m3u(request, pk):
    playlist = get_object_or_404(Playlist, pk=pk, deleted_at=None)
    response = HttpResponse(
        render_m3u(playlist),
        content_type="audio/x-mpegurl",
    )
    response["Content-Disposition"] = f'attachment; filename="{_safe_filename(playlist.name)}.m3u"'
    return response


@login_required
def download_all_m3u(request):
    playlists = Playlist.objects.filter(deleted_at=None)
    return FileResponse(
        BytesIO(render_m3u_zip(playlists)),
        as_attachment=True,
        filename="kuratorr-playlists.zip",
        content_type="application/zip",
    )


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
    return redirect("settings")
