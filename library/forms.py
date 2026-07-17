from pathlib import Path

from django import forms

from library.models import LibraryRoot


class LibraryRootForm(forms.ModelForm):
    class Meta:
        model = LibraryRoot
        fields = ["path", "enabled"]
        widgets = {"path": forms.HiddenInput()}

    def clean_path(self):
        path = Path(self.cleaned_data["path"]).expanduser().resolve()
        if not path.is_dir():
            raise forms.ValidationError("This directory is not accessible to the service.")
        return str(path)
