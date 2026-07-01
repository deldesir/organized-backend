"""URL configuration for the organized backend API.

Routes matching the upstream sws2apps-api v3 contract.
"""

from django.urls import path
from api.views import (
    auth,
    users,
    pockets,
    congregation,
    schedule,
    visiting_speakers,
    webhooks,
)

urlpatterns = [
    # === Auth ===
    path("user-login", auth.LoginView.as_view(), name="user-login"),
    path("users/validate-me", auth.ValidateUserView.as_view(), name="validate-me"),
    path("users/logout", auth.LogoutView.as_view(), name="user-logout"),
    # Passwordless / email / MFA flows — local cookie auth has no Firebase, so
    # these are safe no-ops that return a shape the client accepts.
    path("user-passwordless-login",
         users.PasswordlessLoginView.as_view(), name="passwordless-login"),
    path("user-passwordless-verify",
         users.PasswordlessVerifyView.as_view(), name="passwordless-verify"),
    path("verify-email-token",
         users.VerifyEmailTokenView.as_view(), name="verify-email-token"),
    path("mfa/verify-token",
         users.MfaVerifyTokenView.as_view(), name="mfa-verify-token"),

    # === Public (no auth) ===
    path("public/feature-flags", congregation.FeatureFlagsView.as_view(),
         name="feature-flags"),

    # === Users (VIP) ===
    path("users/<str:id>/backup", users.UserBackupView.as_view(), name="user-backup"),
    path("users/<str:id>/backup/chunked",
         users.ChunkedBackupView.as_view(), name="chunked-backup"),
    path("users/<str:id>/updates-routine",
         users.UserUpdatesView.as_view(), name="user-updates"),
    path("users/<str:id>/sessions",
         users.UserSessionsView.as_view(), name="user-sessions"),
    path("users/<str:id>/erase", users.UserEraseView.as_view(), name="user-erase"),
    path("users/<str:id>/feedback",
         users.UserFeedbackView.as_view(), name="user-feedback"),
    path("users/<str:id>/field-service-reports",
         users.UserFieldServiceReportsView.as_view(), name="user-fs-reports"),
    path("users/<str:id>/applications",
         users.UserApplicationsView.as_view(), name="user-applications"),
    path("users/<str:id>/join-congregation",
         users.UserJoinCongregationView.as_view(), name="user-join-cong"),
    # 2fa/disable must precede 2fa is unnecessary (distinct segment counts).
    path("users/<str:id>/2fa/disable",
         users.User2FADisableView.as_view(), name="user-2fa-disable"),
    path("users/<str:id>/2fa", users.User2FAView.as_view(), name="user-2fa"),

    # === Pockets (publisher) ===
    path("pockets/signup", pockets.PocketSignupView.as_view(), name="pocket-signup"),
    path("pockets/validate-me",
         pockets.ValidatePocketView.as_view(), name="pocket-validate"),
    path("pockets/backup", pockets.PocketBackupView.as_view(), name="pocket-backup"),
    path("pockets/applications",
         pockets.PocketApplicationsView.as_view(), name="pocket-applications"),
    path("pockets/erase", pockets.PocketEraseView.as_view(), name="pocket-erase"),
    path("pockets/field-service-reports",
         pockets.PocketFieldServiceReportView.as_view(), name="pocket-fs-reports"),
    path("pockets/sessions",
         pockets.PocketSessionsView.as_view(), name="pocket-sessions"),
    path("sws-pocket/meeting-schedule",
         schedule.PocketMeetingScheduleView.as_view(), name="pocket-meeting-schedule"),

    # === Congregations ===
    path("congregations/", congregation.CreateCongregationView.as_view(),
         name="create-congregation"),
    path("congregations/countries", congregation.CountriesView.as_view(),
         name="countries"),
    path("congregations/search", congregation.SearchCongregationsView.as_view(),
         name="search-congregations"),

    # === Congregation admin ===
    # NB: /users/global MUST precede /users/<user_id> or "global" is captured
    # as a user_id; /users/<user_id>/sessions is distinct by segment count.
    path("congregations/admin/<str:id>/users",
         congregation.CongUsersView.as_view(), name="cong-users"),
    path("congregations/admin/<str:id>/members",
         congregation.CongUsersView.as_view(), name="cong-members"),
    path("congregations/admin/<str:id>/users/global",
         congregation.CongUserGlobalSearchView.as_view(), name="cong-users-global"),
    path("congregations/admin/<str:id>/users/<str:user_id>/sessions",
         congregation.CongUserSessionsView.as_view(), name="cong-user-sessions"),
    path("congregations/admin/<str:id>/users/<str:user_id>",
         congregation.CongUserDetailView.as_view(), name="cong-user-detail"),
    path("congregations/admin/<str:id>/pocket-user",
         congregation.PocketUserAddView.as_view(), name="pocket-user-add"),
    path("congregations/admin/<str:id>/pocket-user/<str:user_id>",
         congregation.PocketUserDeleteView.as_view(), name="pocket-user-delete"),
    path("congregations/admin/<str:id>/master-key",
         congregation.MasterKeyView.as_view(), name="master-key"),
    path("congregations/admin/<str:id>/access-code",
         congregation.AccessCodeView.as_view(), name="access-code"),
    path("congregations/admin/<str:id>/local-uid",
         congregation.LocalUidView.as_view(), name="local-uid"),
    path("congregations/admin/<str:id>/erase",
         congregation.CongregationEraseView.as_view(), name="cong-erase"),
    path("congregations/admin/<str:id>/join-requests",
         congregation.JoinRequestsView.as_view(), name="cong-join-requests"),

    # === Congregation meeting / schedules / visiting speakers ===
    path("congregations/meeting/<str:id>/schedules",
         schedule.PublishedSchedulesView.as_view(), name="published-schedules"),
    path("congregations/meeting/<str:id>/visiting-speakers/access",
         visiting_speakers.ApprovedAccessView.as_view(), name="vs-access"),
    path("congregations/meeting/<str:id>/visiting-speakers/congregations",
         visiting_speakers.FindCongregationsView.as_view(), name="vs-congregations"),
    path("congregations/meeting/<str:id>/visiting-speakers/pending-access",
         visiting_speakers.PendingAccessView.as_view(), name="vs-pending-access"),
    path("congregations/meeting/<str:id>/visiting-speakers/request/approve",
         visiting_speakers.ApproveRequestView.as_view(), name="vs-request-approve"),
    path("congregations/meeting/<str:id>/visiting-speakers/request/reject",
         visiting_speakers.RejectRequestView.as_view(), name="vs-request-reject"),
    path("congregations/meeting/<str:id>/visiting-speakers/request",
         visiting_speakers.RequestAccessView.as_view(), name="vs-request"),

    # === Generic congregation (after the literal admin/ and meeting/ prefixes) ===
    path("congregations/<str:id>/meeting-schedule",
         schedule.CongregationMeetingScheduleView.as_view(),
         name="congregation-meeting-schedule"),
    path("congregations/<str:id>/applications/<str:app_id>",
         congregation.CongregationApplicationView.as_view(),
         name="congregation-application"),

    # === Webhooks (Phase 3 — shared secret auth) ===
    path("webhooks/assignment-response",
         webhooks.AssignmentResponseWebhook.as_view(), name="webhook-assignment"),
    path("webhooks/query",
         webhooks.ScheduleQueryWebhook.as_view(), name="webhook-query"),
    # Legacy alias — keep until all callers are updated
    path("webhooks/hermes",
         webhooks.ScheduleQueryWebhook.as_view(), name="webhook-hermes"),
]
