"""User backup views: GET and POST for VIP users.

Replicates the upstream /users/:id/backup endpoints.
"""

import json
import logging

from django.core.cache import cache
from django.http import JsonResponse
from django.views import View

from api.services.backup import retrieve_user_backup, save_user_backup
from api.services.cookies import verify_visitor_cookie

logger = logging.getLogger('organized.backup')


class UserBackupView(View):
    """GET/POST /api/v3/users/<id>/backup

    GET: Return role-scoped backup data with metadata-based incremental sync.
    POST: Save backup with conflict detection (BACKUP_OUTDATED).
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        metadata_str = request.META.get('HTTP_METADATA', '{}')
        try:
            incoming_metadata = json.loads(metadata_str)
        except json.JSONDecodeError:
            incoming_metadata = {}

        result = retrieve_user_backup(cong_user, incoming_metadata)
        return JsonResponse(result)

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        cong_backup = body.get('cong_backup', body)
        success, error = save_user_backup(cong_user, cong_backup)

        if not success:
            return JsonResponse({'message': error}, status=400)

        return JsonResponse({'message': 'BACKUP_SENT'})


class ChunkedBackupView(View):
    """POST /api/v3/users/<id>/backup/chunked

    Accepts chunked backup uploads. Reassembles and processes
    when the final chunk arrives.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        upload_id = body.get('uploadId')
        chunk_index = body.get('chunkIndex')
        total_chunks = body.get('totalChunks')
        chunk_data = body.get('chunkData')

        if not all(x is not None for x in [upload_id, chunk_index, total_chunks, chunk_data]):
            return JsonResponse({'message': 'MISSING_PARAMETERS'}, status=400)

        # Store chunk in cache
        cache_key = f"upload_{cong_user.id}_{upload_id}_chunk_{chunk_index}"
        cache.set(cache_key, chunk_data, timeout=3600)

        # Reassemble if this is the last chunk
        if chunk_index == total_chunks - 1:
            full_json_str = ""
            for i in range(total_chunks):
                chunk = cache.get(f"upload_{cong_user.id}_{upload_id}_chunk_{i}")
                if chunk is None:
                    return JsonResponse(
                        {'message': f'MISSING_CHUNK_{i}'}, status=400
                    )
                full_json_str += chunk

            try:
                payload = json.loads(full_json_str)
            except json.JSONDecodeError:
                return JsonResponse({'message': 'INVALID_JSON'}, status=400)

            cong_backup = payload.get('cong_backup', payload)
            success, error = save_user_backup(cong_user, cong_backup)

            # Clean up cache
            for i in range(total_chunks):
                cache.delete(f"upload_{cong_user.id}_{upload_id}_chunk_{i}")

            if not success:
                return JsonResponse({'message': error}, status=409)

            return JsonResponse({'message': 'BACKUP_SENT'})

        return JsonResponse({'message': 'CHUNK_UPLOADED'})


class UserUpdatesView(View):
    """GET /api/v3/users/<id>/updates-routine

    Returns pending updates for the user (e.g., schedule changes).
    Simplified version — returns empty for now.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'USER_ID_INVALID'}, status=400)

        return JsonResponse({'updates': []})


def _session_to_response(session, current_visitor_id):
    """Map a stored session object to the frontend SessionResponseType shape.

    Stored sessions (see auth.LoginView) carry visitorid/identifier/last_seen.
    ip/country_name/device are not collected locally, so they default to safe
    placeholders — every key the SessionResponseType requires is always present
    and correctly typed so the client never crashes on a missing field.
    """
    device = session.get('device') or {}
    return {
        'identifier': session.get('identifier', ''),
        'isSelf': session.get('visitorid') == current_visitor_id,
        'ip': session.get('ip', ''),
        'country_name': session.get('country_name', ''),
        'device': {
            'browserName': device.get('browserName', ''),
            'os': device.get('os', ''),
            'isMobile': bool(device.get('isMobile', False)),
        },
        'last_seen': session.get('last_seen', ''),
    }


class UserSessionsView(View):
    """GET/DELETE /api/v3/users/<id>/sessions

    GET returns the authenticated user's active sessions as a bare JSON array
    (the frontend wraps it into result.sessions itself). DELETE revokes one
    session by its identifier. Both require the caller to be the same user.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        current_visitor_id = verify_visitor_cookie(request)
        sessions = [
            _session_to_response(s, current_visitor_id)
            for s in (cong_user.sessions or [])
        ]
        # Bare array — apiGetUserSessions reads `data` directly as the list.
        return JsonResponse(sessions, safe=False)

    def delete(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        identifier = body.get('identifier', '')
        cong_user.sessions = [
            s for s in (cong_user.sessions or [])
            if s.get('identifier') != identifier
        ]
        cong_user.save(update_fields=['sessions'])

        return JsonResponse({'message': 'SESSION_REVOKED'})


class UserEraseView(View):
    """DELETE /api/v3/users/<id>/erase

    Permanently deletes the user account. Deleting the CongUser cascades to its
    UserBackupTable/CongBackupTable-equivalent (UserBackupTable, Metadata) rows;
    the linked Django auth user is removed too so the login is fully erased.
    """

    def delete(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        auth_user = cong_user.auth_user
        cong_user.delete()  # cascades UserBackupTable + Metadata via FK
        if auth_user is not None:
            auth_user.delete()

        return JsonResponse({'message': 'Account deleted'})


class UserFeedbackView(View):
    """POST /api/v3/users/<id>/feedback

    Feedback submission. Not critical to a local install, so this is a safe
    no-op that just logs for admin review and always returns 200 so the UI
    never blocks on it.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        subject = body.get('subject', '')
        message = body.get('message', '')
        logger.info(
            f"Feedback from {cong_user.firstname} {cong_user.lastname} "
            f"({cong_user.id}): {subject!r} — {message!r}"
        )

        return JsonResponse({'message': 'Feedback received'})


class UserFieldServiceReportsView(View):
    """POST /api/v3/users/<id>/field-service-reports

    A VIP publisher submits (or withdraws) their own pre-encrypted field
    service report. The report is destined for the SECRETARY — the client
    encrypts it for the 'incoming_reports' table (useSubmitReport /
    useWithdrawReport) exactly like the pocket flow — so it is appended to the
    congregation's incoming_reports list, NOT the submitter's own backup.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from django.utils import timezone

        from api.models import Metadata

        # Mirror PocketFieldServiceReportView: append the report to the
        # congregation's incoming_reports (replacing any prior entry with the
        # same person_uid + report_month) so the secretary's next backup GET
        # re-sends it. `person_uid` and `report_month` stay plaintext for
        # reconciliation; a withdrawal arrives with the same key and an
        # (encrypted) `_deleted` flag, so store it opaquely like any submission.
        # The previous implementation dumped the report into the submitter's own
        # user_field_service_reports table, where no secretary would ever see it.
        report = body.get('report') or {}
        cong = cong_user.congregation

        reports = (
            cong.incoming_reports
            if isinstance(cong.incoming_reports, list) else []
        )
        key = (report.get('person_uid'), report.get('report_month'))
        reports = [
            r for r in reports
            if (r.get('person_uid'), r.get('report_month')) != key
        ]
        reports.append(report)

        cong.incoming_reports = reports
        cong.save(update_fields=['incoming_reports'])

        # The report's own `updatedAt`/`_deleted` are client-encrypted (not
        # comparable), so use a server timestamp for the cong-scoped sync
        # metadata that gates the secretary's incoming_reports re-send.
        ts = timezone.now().isoformat()
        Metadata.objects.update_or_create(
            congregation=cong, cong_user=None, key='incoming_reports',
            defaults={'value': ts}
        )

        return JsonResponse({'message': 'REPORT_SENT'})


class UserApplicationsView(View):
    """GET/POST /api/v3/users/<id>/applications

    GET returns the user's application records as a bare JSON array
    (apiUserGetApplications reads `data` directly as APRecordType[]). POST
    appends a new (client-encrypted) application to the same backup table.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        from api.models import UserBackupTable

        try:
            bt = UserBackupTable.objects.get(
                cong_user=cong_user, table_name='applications'
            )
            data = bt.data if isinstance(bt.data, list) else []
        except UserBackupTable.DoesNotExist:
            data = []

        # Bare array — empty collections stay a correctly-typed [].
        return JsonResponse(data, safe=False)

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from django.utils import timezone

        from api.models import Metadata, UserBackupTable

        application = body.get('application') or {}
        cong = cong_user.congregation

        try:
            bt = UserBackupTable.objects.get(
                cong_user=cong_user, table_name='applications'
            )
            data = bt.data if isinstance(bt.data, list) else []
        except UserBackupTable.DoesNotExist:
            data = []

        # De-duplicate on the (plaintext) submitted timestamp so a resubmit
        # replaces rather than duplicates — mirrors PocketApplicationsView.
        submitted = application.get('submitted')
        if submitted:
            data = [
                a for a in data
                if not (isinstance(a, dict) and a.get('submitted') == submitted)
            ]
        data.append(application)

        UserBackupTable.objects.update_or_create(
            cong_user=cong_user, table_name='applications',
            defaults={'data': data}
        )

        ts = timezone.now().isoformat()
        Metadata.objects.update_or_create(
            congregation=cong, cong_user=cong_user, key='applications',
            defaults={'value': ts}
        )

        return JsonResponse({'message': 'APPLICATION_SENT'})


class UserJoinCongregationView(View):
    """POST /api/v3/users/<id>/join-congregation

    Submits a request to join an existing congregation. Searches by name (and
    country when provided), records the request in Congregation.data, and
    updates the user's firstname/lastname when supplied.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or str(cong_user.id) != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from django.utils import timezone

        from api.models import Congregation

        cong_name = (body.get('cong_name') or '').strip()
        country_code = (body.get('country_code') or '').strip()
        firstname = body.get('firstname') or ''
        lastname = body.get('lastname') or ''

        if not cong_name:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        # Update the user's profile names if provided.
        changed = False
        if firstname:
            cong_user.firstname = firstname
            changed = True
        if lastname:
            cong_user.lastname = lastname
            changed = True
        if changed:
            cong_user.save(update_fields=['firstname', 'lastname'])

        qs = Congregation.objects.filter(cong_name__iexact=cong_name)
        if country_code:
            qs = qs.filter(country_code__iexact=country_code)
        cong = qs.first()
        if cong is None:
            return JsonResponse(
                {'message': 'CONGREGATION_NOT_FOUND'}, status=400
            )

        # Record the join request additively in the congregation JSON blob.
        data = cong.data if isinstance(cong.data, dict) else {}
        join_requests = data.get('join_requests')
        if not isinstance(join_requests, list):
            join_requests = []
        join_requests.append({
            'user_id': str(cong_user.id),
            'firstname': firstname,
            'lastname': lastname,
            'country_code': country_code,
            'requested_at': timezone.now().isoformat(),
        })
        data['join_requests'] = join_requests
        cong.data = data
        cong.save(update_fields=['data'])

        return JsonResponse({'message': 'JOIN_REQUEST_SENT'})


# ---------------------------------------------------------------------------
# Auth-flow stubs. Local auth uses a signed visitorid cookie (base64
# email:password Bearer at login) — there is no Firebase 2FA / passwordless /
# email-verification flow. These endpoints exist only so the matching client
# code paths receive a well-formed 200 and never crash; they perform no auth
# state changes.
# ---------------------------------------------------------------------------


class User2FAView(View):
    """GET /api/v3/users/<id>/2fa

    No-op: local auth has no Firebase 2FA. Reports MFA as disabled.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        return JsonResponse({'mfaEnabled': False})


class User2FADisableView(View):
    """GET /api/v3/users/<id>/2fa/disable

    No-op: local auth has no Firebase 2FA to disable.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        return JsonResponse({'message': '2FA not enabled'})


class MfaVerifyTokenView(View):
    """POST /api/v3/mfa/verify-token

    No-op: local auth has no MFA token to verify (Firebase 2FA concept).
    Returns the full app_settings envelope (UserLoginResponseType shape) built
    from the authenticated user so any client code path that calls it succeeds.
    """

    def post(self, request, id=None):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        settings = cong.get_settings_for_user(cong_user)

        return JsonResponse({
            'message': 'Token verified',
            'app_settings': {
                'user_settings': {
                    'firstname': cong_user.firstname,
                    'lastname': cong_user.lastname,
                    'cong_role': cong_user.cong_role,
                    'user_local_uid': cong_user.user_local_uid,
                    'user_members_delegate': cong_user.user_members_delegate,
                },
                'cong_settings': {
                    'id': cong.cong_id,
                    'country_code': cong.country_code,
                    'cong_name': cong.cong_name,
                    'cong_master_key': settings.get('cong_master_key') or '',
                    'cong_access_code': settings.get('cong_access_code') or '',
                },
            },
        })


class PasswordlessLoginView(View):
    """POST /api/v3/user-passwordless-login (public, exempt in middleware)

    No-op: local auth uses base64(email:password) Bearer, not Firebase
    passwordless email links. Returns a 200 stub the client accepts.
    """

    def post(self, request):
        return JsonResponse(
            {'message': 'Passwordless login not supported'}
        )


class PasswordlessVerifyView(View):
    """POST /api/v3/user-passwordless-verify

    No-op: local auth has no passwordless flow to verify. The caller is already
    authenticated via the visitorid cookie, so just acknowledge.
    """

    def post(self, request):
        return JsonResponse({'message': 'Already authenticated'})


class VerifyEmailTokenView(View):
    """POST /api/v3/verify-email-token (public, exempt in middleware)

    No-op: local auth does not use Firebase email verification. Returns a 200
    stub the client accepts.
    """

    def post(self, request):
        return JsonResponse(
            {'message': 'Email verification not supported'}
        )
