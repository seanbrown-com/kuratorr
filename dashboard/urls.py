from django.urls import path

from dashboard import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("settings/", views.settings_view, name="settings"),
    path("jobs/", views.job_history, name="job-history"),
    path("jobs/<str:job_type>/run/", views.run_job, name="run-job"),
    path("health/", views.health, name="health"),
]
