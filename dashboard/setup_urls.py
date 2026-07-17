from django.urls import path

from dashboard.views import initial_setup

urlpatterns = [path("", initial_setup, name="initial-setup")]
