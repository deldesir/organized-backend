"""Pocket (publisher) views.

Pocket users authenticate via invitation codes, not email/password.
They get minimal data: public schedules, their own reports.
"""

import json
import logging
import uuid

from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from api.services.backup import retrieve_user_backup, save_user_backup
from api.services.cookies import sign_visitor_cookie, verify_visitor_cookie

logger = logging.getLogger('organized.pocket')


class PocketSignupView(View):
    """POST /api/v3/pockets/signup

    Validates an invitation code and creates a session.
    """

    def post(self, request):
        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        code = body.get('code', '')
        if not code:
            return JsonResponse({'message': 'INVITATION_CODE_INVALID'}, status=403)

        from api.models import CongUser

        try:
            cong_user = CongUser.objects.select_related('congregation').get(
                pocket_invitation_code=code
            )
        except CongUser.DoesNotExist:
            return JsonResponse({'message': 'INVITATION_CODE_INVALID'}, status=403)

        # Create session
        visitor_id = str(uuid.uuid4())
        session = {
            'mfaVerified': False,
            'last_seen': timezone.now().isoformat(),
            'visitorid': visitor_id,
            'identifier': str(uuid.uuid4()),
        }

        sessions = cong_user.sessions or []
        sessions.append(session)
        cong_user.sessions = sessions
        cong_user.save(update_fields=['sessions'])

        cong = cong_user.congregation

        response_data = {
            'message': 'POCKET_SETUP_DONE',
            'id': str(cong_user.id),
            'app_settings': {
                'user_settings': {
                    'firstname': cong_user.firstname,
                    'lastname': cong_user.lastname,
                    'cong_role': cong_user.cong_role,
                    'user_local_uid': cong_user.user_local_uid,
                    'user_members_delegate': cong_user.user_members_delegate,
                },
                'cong_settings': cong.get_settings_for_user(cong_user),
            },
        }

        response = JsonResponse(response_data)
        response = sign_visitor_cookie(response, visitor_id)
        return response


class ValidatePocketView(View):
    """GET /api/v3/pockets/validate-me"""

    def get(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        cong = cong_user.congregation
        return JsonResponse({
            'message': 'TOKEN_VALID',
            'id': str(cong_user.id),
            'app_settings': {
                'user_settings': {
                    'firstname': cong_user.firstname,
                    'lastname': cong_user.lastname,
                    'cong_role': cong_user.cong_role,
                    'user_local_uid': cong_user.user_local_uid,
                    'user_members_delegate': cong_user.user_members_delegate,
                },
                'cong_settings': cong.get_settings_for_user(cong_user),
            },
        })


class PocketBackupView(View):
    """GET/POST /api/v3/pockets/backup"""

    def get(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        metadata_str = request.META.get('HTTP_METADATA', '{}')
        try:
            incoming_metadata = json.loads(metadata_str)
        except json.JSONDecodeError:
            incoming_metadata = {}

        result = retrieve_user_backup(cong_user, incoming_metadata)
        return JsonResponse(result)

    def post(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        cong_backup = body.get('cong_backup', body)
        success, error = save_user_backup(cong_user, cong_backup)

        if not success:
            return JsonResponse({'message': error}, status=400)

        return JsonResponse({'message': 'BACKUP_SENT'})


class PocketApplicationsView(View):
    """GET/POST /api/v3/pockets/applications

    GET  — return the publisher's own application records (encrypted by the
           client) from their user-scoped 'applications' backup table — the
           same table POST writes to, so a submission (and any status set on
           it) round-trips back to the form.
    POST — a publisher submits their own (pre-encrypted) application. Stored
           user-scoped so each publisher owns their submissions.
    """

    def get(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        from api.models import UserBackupTable

        # Mirror UserApplicationsView: read the publisher's OWN (user-scoped)
        # applications — the same table POST writes to. Reading a
        # congregation-scoped table here would always yield [] (nothing ever
        # populates one) and would leak other publishers' submissions.
        try:
            bt = UserBackupTable.objects.get(
                cong_user=cong_user, table_name='applications'
            )
            data = bt.data if isinstance(bt.data, list) else []
        except UserBackupTable.DoesNotExist:
            data = []

        # Frontend consumes this raw array directly (pocket.ts line 136).
        return JsonResponse(data, safe=False)

    def post(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from api.models import UserBackupTable, Metadata

        application = body.get('application') or {}
        cong = cong_user.congregation

        # Append to the publisher's own (user-scoped) applications list,
        # de-duplicating on the (unencrypted) submitted timestamp so a
        # resubmit replaces rather than duplicates.
        try:
            bt = UserBackupTable.objects.get(
                cong_user=cong_user, table_name='applications'
            )
            data = bt.data if isinstance(bt.data, list) else []
        except UserBackupTable.DoesNotExist:
            data = []

        submitted = application.get('submitted')
        if submitted:
            data = [a for a in data if a.get('submitted') != submitted]
        data.append(application)

        UserBackupTable.objects.update_or_create(
            cong_user=cong_user, table_name='applications',
            defaults={'data': data}
        )

        # `submitted` is client-encrypted, so it is unusable as a comparable
        # sync timestamp — use a server timestamp instead.
        ts = timezone.now().isoformat()
        Metadata.objects.update_or_create(
            congregation=cong, cong_user=cong_user, key='applications',
            defaults={'value': ts}
        )

        return JsonResponse({'message': 'APPLICATIONS_SENT'})


class PocketFieldServiceReportView(View):
    """POST /api/v3/pockets/field-service-reports

    A publisher submits (or withdraws) their own pre-encrypted field service
    report. Reports are appended to the congregation's incoming_reports list,
    replacing any existing entry with the same (person_uid, report_month).
    A withdrawal arrives as a report whose (encrypted) `_deleted` flag is set;
    it is stored like any other and the secretary's client decrypts and
    reconciles it on the next backup sync.
    """

    def post(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from api.models import Metadata

        report = body.get('report') or {}
        cong = cong_user.congregation

        reports = cong.incoming_reports if isinstance(cong.incoming_reports, list) else []
        key = (report.get('person_uid'), report.get('report_month'))
        reports = [
            r for r in reports
            if (r.get('person_uid'), r.get('report_month')) != key
        ]
        # Store the report opaquely. `_deleted` is client-encrypted here, so the
        # server cannot interpret it: its encrypted form is a non-empty string
        # (truthy) for BOTH submissions and withdrawals. Gating the append on it
        # dropped every normal submission. Instead append unconditionally (after
        # replacing any prior entry with the same person_uid + report_month);
        # the secretary's client decrypts `_deleted` and reconciles withdrawals.
        reports.append(report)

        cong.incoming_reports = reports
        cong.save(update_fields=['incoming_reports'])

        # Bump the congregation-scoped sync timestamp so the secretary's next
        # backup GET re-sends incoming_reports. Use a server timestamp because
        # the report's own `updatedAt` is client-encrypted (not comparable).
        ts = timezone.now().isoformat()
        Metadata.objects.update_or_create(
            congregation=cong, cong_user=None, key='incoming_reports',
            defaults={'value': ts}
        )

        return JsonResponse({'message': 'FIELD_SERVICE_REPORT_SENT'})


class PocketSessionsView(View):
    """GET/DELETE /api/v3/pockets/sessions

    GET    — list the publisher's active sessions, flagging the current one.
    DELETE — revoke a session by its identifier (idempotent).
    """

    def get(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        current_visitor = verify_visitor_cookie(request)

        sessions = []
        for s in (cong_user.sessions or []):
            device = s.get('device')
            if not isinstance(device, dict):
                device = {}
            sessions.append({
                'identifier': s.get('identifier', ''),
                'isSelf': s.get('visitorid') == current_visitor,
                'ip': s.get('ip', ''),
                'country_name': s.get('country_name', ''),
                'device': {
                    'browserName': device.get('browserName', ''),
                    'os': device.get('os', ''),
                    'isMobile': device.get('isMobile', False),
                },
                'last_seen': s.get('last_seen', ''),
            })

        # Frontend treats a bare array as {sessions}; must be [] never null.
        return JsonResponse(sessions, safe=False)

    def delete(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        identifier = body.get('identifier', '')

        # Remove the matching session; idempotent if not present.
        cong_user.sessions = [
            s for s in (cong_user.sessions or [])
            if s.get('identifier') != identifier
        ]
        cong_user.save(update_fields=['sessions'])

        return JsonResponse({'message': 'SESSION_REVOKED'})


class PocketEraseView(View):
    """DELETE /api/v3/pockets/erase

    Hard-delete the pocket account. Cascade removes UserBackupTable and
    Metadata rows; sessions live on the CongUser and go with it. The
    visitorid cookie is cleared so the client is signed out immediately.
    """

    def delete(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        cong_user.delete()

        response = JsonResponse({'message': 'POCKET_ACCOUNT_DELETED'})
        response.delete_cookie('visitorid', path='/')
        return response
