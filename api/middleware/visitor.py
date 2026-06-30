"""Visitor checker middleware.

Validates the signed visitorid cookie on every API request,
loads the corresponding CongUser, and attaches it to the request.

Exempt paths (login, signup, public endpoints) skip validation.
"""

import json
import logging

from django.http import JsonResponse
from django.utils import timezone

from api.services.cookies import verify_visitor_cookie

logger = logging.getLogger('organized.auth')

# Paths that do NOT require cookie authentication
EXEMPT_PATHS = [
    '/api/v3/user-login',
    '/api/v3/user-passwordless-login',
    '/api/v3/verify-email-token',
    '/api/v3/pockets/signup',
    '/api/v3/congregations/countries',
    '/api/v3/congregations/search',
    '/api/v3/public/',  # Public app config (feature flags); no auth
    '/api/v3/webhooks/',  # Webhooks use shared secret auth
]


class VisitorCheckerMiddleware:
    """Authenticate API requests via signed visitorid cookie.

    Sets request.cong_user to the authenticated CongUser instance,
    or None for exempt paths.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.cong_user = None

        path = request.path

        # Skip non-API paths (static files, frontend, admin)
        if not path.startswith('/api/v3/'):
            return self.get_response(request)

        # Skip exempt paths
        if any(path.startswith(p) for p in EXEMPT_PATHS):
            return self.get_response(request)

        # Validate signed cookie
        visitor_id = verify_visitor_cookie(request)
        if not visitor_id:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        # Find user with this active session
        from api.models import CongUser

        try:
            # JSONField contains query: find user whose sessions array
            # contains an object with matching visitorid
            cong_user = CongUser.objects.select_related('congregation').get(
                sessions__contains=[{'visitorid': visitor_id}]
            )
        except CongUser.DoesNotExist:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)
        except CongUser.MultipleObjectsReturned:
            logger.error(f"Multiple users with visitorid {visitor_id}")
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        # Update last_seen on the matching session
        sessions_modified = False
        for session in cong_user.sessions:
            if session.get('visitorid') == visitor_id:
                session['last_seen'] = timezone.now().isoformat()
                sessions_modified = True
                break

        if sessions_modified:
            cong_user.save(update_fields=['sessions'])

        request.cong_user = cong_user
        return self.get_response(request)
