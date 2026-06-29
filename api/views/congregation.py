"""Congregation management views."""

import json
import logging
import uuid

from django.contrib.auth.models import User
from django.http import JsonResponse
from django.views import View

logger = logging.getLogger('organized.congregation')


class CreateCongregationView(View):
    """PUT /api/v3/congregations/

    First-time congregation setup by an admin user.
    """

    def put(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'TOKEN_INVALID'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from api.models import Congregation

        cong_name = body.get('cong_name', '')
        cong_number = body.get('cong_number', '')
        country_code = body.get('country_code', '')

        if not cong_name:
            return JsonResponse({'message': 'CONG_NAME_REQUIRED'}, status=400)

        cong, created = Congregation.objects.get_or_create(
            cong_number=cong_number,
            defaults={
                'cong_id': str(uuid.uuid4()),
                'cong_name': cong_name,
                'country_code': country_code,
                'cong_settings': body.get('cong_settings', {}),
            }
        )

        if not created:
            return JsonResponse({'message': 'CONG_EXISTS'}, status=409)

        # Link the creating user to the congregation
        cong_user.congregation = cong
        if 'admin' not in (cong_user.cong_role or []):
            cong_user.cong_role = list(cong_user.cong_role or []) + ['admin']
        cong_user.save()

        return JsonResponse({
            'message': 'CONG_CREATED',
            'cong_id': cong.cong_id,
        })


class CongUsersView(View):
    """GET/POST /api/v3/congregations/admin/<id>/users"""

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        members = cong.members.all()

        users_list = [
            {
                'id': str(m.id),
                'firstname': m.firstname,
                'lastname': m.lastname,
                'cong_role': m.cong_role,
                'user_local_uid': m.user_local_uid,
                'pocket': m.pocket_invitation_code is not None,
            }
            for m in members
        ]

        return JsonResponse({'users': users_list})

    def post(self, request, id):
        """Add a new VIP user to the congregation."""
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from api.models import CongUser

        email = body.get('email', '')
        firstname = body.get('firstname', '')
        lastname = body.get('lastname', '')
        cong_role = body.get('cong_role', ['publisher'])

        if not email:
            return JsonResponse({'message': 'EMAIL_REQUIRED'}, status=400)

        # Create or get Django auth user
        auth_user, _ = User.objects.get_or_create(
            username=email,
            defaults={'email': email}
        )

        cong = cong_user.congregation
        new_user, created = CongUser.objects.get_or_create(
            auth_user=auth_user,
            defaults={
                'congregation': cong,
                'firstname': firstname,
                'lastname': lastname,
                'cong_role': cong_role,
            }
        )

        if not created:
            return JsonResponse({'message': 'USER_EXISTS'}, status=409)

        return JsonResponse({
            'message': 'USER_ADDED',
            'id': str(new_user.id),
        })


class PocketUserAddView(View):
    """POST /api/v3/congregations/admin/<id>/pocket-user"""

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from api.models import CongUser

        firstname = body.get('firstname', '')
        lastname = body.get('lastname', '')

        # Generate invitation code: PREFIX-XXXX-XXXX
        cong = cong_user.congregation
        prefix = cong.cong_prefix or 'LC'
        code = f"{prefix}-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"

        pocket_user = CongUser.objects.create(
            congregation=cong,
            firstname=firstname,
            lastname=lastname,
            cong_role=['publisher'],
            pocket_invitation_code=code,
        )

        return JsonResponse({
            'message': 'POCKET_USER_CREATED',
            'id': str(pocket_user.id),
            'code': code,
        })


class MasterKeyView(View):
    """POST /api/v3/congregations/admin/<id>/master-key"""

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        cong = cong_user.congregation
        settings = cong.cong_settings or {}
        settings['cong_master_key'] = body.get('cong_master_key', '')
        cong.cong_settings = settings
        cong.save(update_fields=['cong_settings'])

        return JsonResponse({'message': 'MASTER_KEY_UPDATED'})


class AccessCodeView(View):
    """POST /api/v3/congregations/admin/<id>/access-code"""

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        cong = cong_user.congregation
        settings = cong.cong_settings or {}
        settings['cong_access_code'] = body.get('cong_access_code', '')
        cong.cong_settings = settings
        cong.save(update_fields=['cong_settings'])

        return JsonResponse({'message': 'ACCESS_CODE_UPDATED'})


class CountriesView(View):
    """GET /api/v3/congregations/countries (public, no auth)"""

    def get(self, request):
        # Return a minimal countries list for local mode
        return JsonResponse({'countries': [
            {'code': 'US', 'name': 'United States'},
            {'code': 'HT', 'name': 'Haiti'},
        ]})


class SearchCongregationsView(View):
    """GET /api/v3/congregations/search (public, no auth)"""

    def get(self, request):
        from api.models import Congregation

        query = request.GET.get('q', '')
        congs = Congregation.objects.filter(cong_name__icontains=query)[:10]

        return JsonResponse({'congregations': [
            {
                'cong_id': c.cong_id,
                'cong_name': c.cong_name,
                'cong_number': c.cong_number,
            }
            for c in congs
        ]})
