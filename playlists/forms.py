from pathlib import Path
from tempfile import NamedTemporaryFile

from django import forms

from playlists.models import PlaylistOutputRoot


class PlaylistOutputRootForm(forms.ModelForm):
    class Meta:
        model = PlaylistOutputRoot
        fields = ["path", "enabled"]

    def clean_path(self):
        raw_path = self.cleaned_data["path"]
        try:
            path = Path(raw_path).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise NotADirectoryError(f"{path} is not a directory")
            with NamedTemporaryFile(
                mode="w",
                prefix=".kuratorr-write-test-",
                dir=path,
                delete=True,
            ) as probe:
                probe.write("Kuratorr write-access test")
                probe.flush()
        except OSError as exc:
            reason = exc.strerror or str(exc)
            raise forms.ValidationError(
                f"Kuratorr cannot create or write to {raw_path}: {reason}. "
                "Grant the kuratorr service account write access and try again."
            ) from exc
        return str(path)
