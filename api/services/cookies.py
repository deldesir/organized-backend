"""Cookie signing service for session management.

Replicates the upstream sws2apps-api's signed cookie approach.
The visitorid cookie authenticates all API requests after login.
"""

from django.conf import settings
from django.core.signing import Signer, BadSignature

COOKIE_NAME = 'visitorid'
COOKIE_OPTIONS = {
    'httponly': True,
    'secure': False,  # IIAB is HTTP-only on LAN
    'samesite': 'Lax',
    'max_age': 60 * 60 * 24 * 30,  # 30 days
    'path': '/',
}

signer = Signer(key=settings.SECRET_KEY, salt='organized-visitor')


def sign_visitor_cookie(response, visitor_id):
    """Set a signed visitorid cookie on the response."""
    signed = signer.sign(visitor_id)
    response.set_cookie(COOKIE_NAME, signed, **COOKIE_OPTIONS)
    return response


def verify_visitor_cookie(request):
    """Extract and verify the visitorid from the request cookie.

    Returns the visitor_id string if valid, None otherwise.
    """
    signed_value = request.COOKIES.get(COOKIE_NAME)
    if not signed_value:
        return None
    try:
        return signer.unsign(signed_value)
    except BadSignature:
        return None
