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
