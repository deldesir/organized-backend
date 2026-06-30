"""URL configuration for the organized backend API.

Routes matching the upstream sws2apps-api v3 contract.
"""

from django.urls import path
from api.views import auth, users, pockets, congregation, webhooks

urlpatterns = [
    # === Auth ===
    path("user-login", auth.LoginView.as_view(), name="user-login"),
    path("users/validate-me", auth.ValidateUserView.as_view(), name="validate-me"),
    path("users/logout", auth.LogoutView.as_view(), name="user-logout"),

    # === Public (no auth) ===
    path("public/feature-flags", congregation.FeatureFlagsView.as_view(),
         name="feature-flags"),

    # === Users (VIP) ===
    path("users/<str:id>/backup", users.UserBackupView.as_view(), name="user-backup"),
    path("users/<str:id>/backup/chunked",
         users.ChunkedBackupView.as_view(), name="chunked-backup"),
    path("users/<str:id>/updates-routine",
         users.UserUpdatesView.as_view(), name="user-updates"),

    # === Pockets (publisher) ===
    path("pockets/signup", pockets.PocketSignupView.as_view(), name="pocket-signup"),
    path("pockets/validate-me",
         pockets.ValidatePocketView.as_view(), name="pocket-validate"),
    path("pockets/backup", pockets.PocketBackupView.as_view(), name="pocket-backup"),

    # === Congregations ===
    path("congregations/", congregation.CreateCongregationView.as_view(),
         name="create-congregation"),
    path("congregations/countries", congregation.CountriesView.as_view(),
         name="countries"),
    path("congregations/search", congregation.SearchCongregationsView.as_view(),
         name="search-congregations"),

    # === Congregation admin ===
    path("congregations/admin/<str:id>/users",
         congregation.CongUsersView.as_view(), name="cong-users"),
    path("congregations/admin/<str:id>/pocket-user",
         congregation.PocketUserAddView.as_view(), name="pocket-user-add"),
    path("congregations/admin/<str:id>/master-key",
         congregation.MasterKeyView.as_view(), name="master-key"),
    path("congregations/admin/<str:id>/access-code",
         congregation.AccessCodeView.as_view(), name="access-code"),

    # === Webhooks (Phase 3 — shared secret auth) ===
    path("webhooks/assignment-response",
         webhooks.AssignmentResponseWebhook.as_view(), name="webhook-assignment"),
    path("webhooks/query",
         webhooks.ScheduleQueryWebhook.as_view(), name="webhook-query"),
    # Legacy alias — keep until all callers are updated
    path("webhooks/hermes",
         webhooks.ScheduleQueryWebhook.as_view(), name="webhook-hermes"),
]
