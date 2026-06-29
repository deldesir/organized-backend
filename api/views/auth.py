"""Auth views: login, validate-me, logout.

These replicate the upstream sws2apps-api auth endpoints using
local Django auth instead of Firebase Auth.
"""

import base64
import logging
import uuid

from django.contrib.auth import authenticate
from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from api.services.cookies import sign_visitor_cookie, verify_visitor_cookie

logger = logging.getLogger('organized.auth')


class LoginView(View):
    """GET /api/v3/user-login

    Upstream: validates Firebase ID token from Authorization header.
    Local: validates base64(email:password) from Authorization header.

    Creates a session, sets signed visitorid cookie, returns user profile.
    """

    def get(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        token = auth_header[7:]

        # Decode as base64(email:password)
        try:
            decoded = base64.b64decode(token).decode('utf-8')
            email, password = decoded.split(':', 1)
        except Exception:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        user = authenticate(request, username=email, password=password)
        if not user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        # Find CongUser profile
        from api.models import CongUser

        try:
            cong_user = CongUser.objects.select_related('congregation').get(
                auth_user=user
            )
        except CongUser.DoesNotExist:
            return JsonResponse({'message': 'ACCOUNT_NOT_FOUND'}, status=404)

        # Create session
        visitor_id = str(uuid.uuid4())
        session = {
            'mfaVerified': False,
            'last_seen': timezone.now().isoformat(),
            'visitorid': visitor_id,
            'identifier': str(uuid.uuid4()),
        }

        sessions = cong_user.sessions or []
        # Remove any existing session with same visitorid
        sessions = [s for s in sessions if s.get('visitorid') != visitor_id]
        sessions.append(session)
        cong_user.sessions = sessions
        cong_user.save(update_fields=['sessions'])

        cong = cong_user.congregation

        # Build response matching upstream shape
        response_data = {
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
        }

        logger.info(f"Login: {cong_user.firstname} {cong_user.lastname} ({cong.cong_name})")

        response = JsonResponse(response_data)
        response = sign_visitor_cookie(response, visitor_id)
        return response


class ValidateUserView(View):
    """GET /api/v3/users/validate-me

    Called on every app load to verify the session is still valid.
    The VisitorCheckerMiddleware has already validated the cookie
    and set request.cong_user.
    """

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


class LogoutView(View):
    """GET /api/v3/users/logout

    Removes the current session from the user's sessions list
    and clears the visitorid cookie.
    """

    def get(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        visitor_id = verify_visitor_cookie(request)
        if visitor_id:
            cong_user.sessions = [
                s for s in (cong_user.sessions or [])
                if s.get('visitorid') != visitor_id
            ]
            cong_user.save(update_fields=['sessions'])

        response = JsonResponse({'message': 'LOGGED_OUT'})
        response.delete_cookie('visitorid', path='/')
        return response
