from django import forms
from django.contrib.auth import password_validation

from library.models import ServiceSettings


class InitialSetupForm(forms.Form):
    token = forms.CharField(
        widget=forms.PasswordInput, help_text="One-time token generated during installation."
    )
    username = forms.CharField(initial="admin", max_length=150)
    password1 = forms.CharField(widget=forms.PasswordInput, label="Password")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm password")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "Passwords do not match.")
        if cleaned.get("password1"):
            password_validation.validate_password(cleaned["password1"])
        return cleaned


class ServiceSettingsForm(forms.ModelForm):
    spotify_client_id = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leave blank to keep the currently configured value.",
    )
    spotify_client_secret = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leave blank to keep the currently configured value.",
    )
    lastfm_api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leave blank to keep the currently configured value.",
    )
    youtube_api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Leave blank to keep the currently configured value.",
    )

    class Meta:
        model = ServiceSettings
        fields = [
            "spotify_max_tracks",
            "spotify_noteworthy_max_rank",
            "lastfm_min_playcount",
            "lastfm_max_tracks",
            "lastfm_noteworthy_max_rank",
            "minimum_playlist_seconds",
            "max_album_genres",
            "spotify_market",
            "youtube_max_results",
            "youtube_auto_accept_confidence",
            "track_match_review_threshold",
            "track_match_auto_accept_threshold",
            "http_user_agent",
        ]

    def clean(self):
        cleaned = super().clean()
        review_threshold = cleaned.get("track_match_review_threshold")
        accept_threshold = cleaned.get("track_match_auto_accept_threshold")
        if review_threshold is not None and accept_threshold is not None:
            if review_threshold >= accept_threshold:
                self.add_error(
                    "track_match_auto_accept_threshold",
                    "The automatic threshold must be greater than the Review threshold.",
                )
        spotify_id = cleaned.get("spotify_client_id")
        spotify_secret = cleaned.get("spotify_client_secret")
        if bool(spotify_id) != bool(spotify_secret):
            message = "Enter both Spotify values together, or leave both blank to keep them."
            self.add_error("spotify_client_id", message)
            self.add_error("spotify_client_secret", message)
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        self.updated_sources = set()
        secret_sources = {
            "spotify_client_id": "spotify",
            "spotify_client_secret": "spotify",
            "lastfm_api_key": "lastfm",
            "youtube_api_key": "youtube",
        }
        for field, source in secret_sources.items():
            value = self.cleaned_data.get(field)
            if value:
                instance.set_secret(field, value)
                self.updated_sources.add(source)
        if commit:
            instance.save()
            self.save_m2m()
        return instance
