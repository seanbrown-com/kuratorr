import posixpath
from pathlib import Path, PurePosixPath

from django import forms

from playlists.models import PlaylistOutputRoot


class PlaylistOutputRootForm(forms.ModelForm):
    class Meta:
        model = PlaylistOutputRoot
        fields = ["path", "music_relative_path", "enabled"]

    def clean_path(self):
        path = Path(self.cleaned_data["path"]).expanduser()
        if not path.is_absolute():
            raise forms.ValidationError("The playlist output path must be absolute.")
        # Do not resolve, stat, create, or probe the path in this web request.
        # Network filesystems can block those calls indefinitely. The playlist
        # materialization job performs filesystem work in its dedicated worker.
        return str(path)

    def clean_music_relative_path(self):
        value = self.cleaned_data["music_relative_path"].strip().replace("\\", "/")
        if not value:
            return ""
        if PurePosixPath(value).is_absolute():
            raise forms.ValidationError("The M3U music path must be relative.")
        return posixpath.normpath(value)
