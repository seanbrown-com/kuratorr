from django.contrib.auth import get_user_model
from django.db import OperationalError, ProgrammingError
from django.shortcuts import redirect


class InitialSetupMiddleware:
    allowed_prefixes = ("/setup/", "/static/", "/health/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            needs_setup = not get_user_model().objects.exists()
        except (OperationalError, ProgrammingError):
            needs_setup = False
        if needs_setup and not request.path.startswith(self.allowed_prefixes):
            return redirect("initial-setup")
        return self.get_response(request)
