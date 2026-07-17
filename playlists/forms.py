from pathlib import Path

from django import forms

from playlists.models import PlaylistOutputRoot


class PlaylistExportForm(forms.Form):
    source_directory = forms.CharField(
        max_length=4096,
        label="Source music directory",
        help_text=(
            "The directory on the computer where the downloaded playlist will be used. "
            "Track paths are written relative to this directory."
        ),
        widget=forms.TextInput(attrs={"placeholder": "/Volumes/Music or D:\\Music"}),
    )


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
