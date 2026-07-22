from pathlib import Path

from django import forms

from playlists.models import PlaylistOutputRoot


class PlaylistOutputRootForm(forms.ModelForm):
    class Meta:
        model = PlaylistOutputRoot
        fields = ["path", "enabled"]

    def clean_path(self):
        path = Path(self.cleaned_data["path"]).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise forms.ValidationError("The output path is not an accessible directory.")
        return str(path)
