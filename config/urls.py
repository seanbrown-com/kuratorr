from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("setup/", include("dashboard.setup_urls")),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("django-admin/", admin.site.urls),
    path("library/", include("library.urls")),
    path("enrichment/", include("enrichment.urls")),
    path("playlists/", include("playlists.urls")),
    path("", include("dashboard.urls")),
]
