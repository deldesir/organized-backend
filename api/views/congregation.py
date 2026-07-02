"""Congregation management views."""

import json
import logging
import uuid

from django.contrib.auth.models import User
from django.http import JsonResponse
from django.views import View

from api.services.cookies import verify_visitor_cookie
from api.services.names import name_field

logger = logging.getLogger('organized.congregation')


# === Serialization helpers ===
#
# The frontend (CongregationUserType in src/definition/api.ts) expects each
# user as {id, profile{...}, sessions[]}. global_role and the 'pocket' flag are
# DERIVED here, never stored. Every endpoint that returns user(s) routes through
# these helpers so the shape is identical everywhere — and sessions is always a
# correctly-typed [] (the user-details "Sessions" tab does currentUser.sessions
# .map() with no guard, so a missing array would crash the client).

def _global_role(member):
    """Derive UserGlobalRoleType.

    Congregation members are either 'pocket' (invitation-code / offline users)
    or 'vip' (regular accounts — INCLUDING congregation admins, elders and
    coordinators). Upstream's third value, 'admin', is a *platform*-admin flag
    with NO frontend consumer: nothing reads global_role === 'admin', while the
    user-details role editor (UserMainRoles + UserAdditionalRights) renders only
    when global_role === 'vip'. Deriving 'admin' from the congregation cong_role
    therefore hid the role-editing UI for every admin (and CreateCongregationView
    stamps 'admin' onto the creator), so a congregation's own admins could never
    have their roles viewed or edited. Congregation membership never maps to the
    platform-admin role here.
    """
    if member.pocket_invitation_code:
        return 'pocket'
    return 'vip'


def _serialize_sessions(member, current_visitor=None):
    """Map CongUser.sessions (arbitrary JSON) to SessionResponseType[].

    Mirrors PocketSessionsView. Stored sessions only carry identifier/
    visitorid/last_seen, so ip/country_name/device default to empty — the
    client reads .ip, .country_name.length, .device.os.length directly.
    """
    sessions = []
    for s in (member.sessions or []):
        device = s.get('device')
        if not isinstance(device, dict):
            device = {}
        sessions.append({
            'identifier': s.get('identifier', ''),
            'isSelf': bool(current_visitor) and s.get('visitorid') == current_visitor,
            'ip': s.get('ip', '') or '',
            'country_name': s.get('country_name', '') or '',
            'device': {
                'browserName': device.get('browserName', '') or '',
                'os': device.get('os', '') or '',
                'isMobile': bool(device.get('isMobile', False)),
            },
            'last_seen': s.get('last_seen', '') or '',
        })
    return sessions


def _serialize_user(member, current_visitor=None):
    """Build one CongregationUserType object."""
    created = member.created_at.isoformat() if member.created_at else ''
    return {
        'id': str(member.id),
        'profile': {
            'global_role': _global_role(member),
            'cong_role': member.cong_role or [],
            'firstname': name_field(member.firstname, created),
            'lastname': name_field(member.lastname, created),
            'user_local_uid': member.user_local_uid or '',
            'user_members_delegate': member.user_members_delegate or [],
            'pocket_invitation_code': member.pocket_invitation_code,
            'createdAt': created,
        },
        'sessions': _serialize_sessions(member, current_visitor),
    }


def _users_list(cong, current_visitor=None):
    """All users of a congregation as CongregationUserType[]."""
    return [_serialize_user(m, current_visitor) for m in cong.members.all()]


def _get_member(cong, user_id):
    """Fetch a CongUser of this congregation by id, tolerating bad UUIDs."""
    try:
        return cong.members.filter(id=user_id).first()
    except Exception:
        return None


class FeatureFlagsView(View):
    """GET /api/v3/public/feature-flags (public, no auth)

    Upstream serves remote feature toggles here. The self-host build reads
    them from the ORGANIZED_FEATURE_FLAGS env var (comma-separated flag
    names, e.g. "HOURGLASS_IMPORT"); the frontend merges the response over
    its build-time VITE_FLAGS_* defaults, so no rebuild is needed to toggle
    a feature.
    """

    def get(self, request):
        import os
        flags = {
            name.strip(): True
            for name in os.getenv('ORGANIZED_FEATURE_FLAGS', '').split(',')
            if name.strip()
        }
        return JsonResponse(flags)


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
        country_code = body.get('country_code', '')
        firstname = body.get('firstname', '')
        lastname = body.get('lastname', '')

        if not cong_name:
            return JsonResponse({'message': 'CONG_NAME_REQUIRED'}, status=400)

        # If this user already owns a congregation, return it idempotently
        # rather than 409ing an onboarding retry. The frontend does not send
        # cong_number, so keying on it made every second congregation collide
        # on '' — key on the creating user instead (single-tenant per account).
        cong = cong_user.congregation
        if cong is None:
            cong = Congregation.objects.create(
                cong_id=str(uuid.uuid4()),
                cong_name=cong_name,
                country_code=country_code,
                cong_number=body.get('cong_number', '') or '',
                cong_settings=body.get('cong_settings', {}),
            )

        # Link the creating user to the congregation and record their name.
        cong_user.congregation = cong
        if firstname:
            cong_user.firstname = name_field(firstname)
        if lastname:
            cong_user.lastname = name_field(lastname)
        if 'admin' not in (cong_user.cong_role or []):
            cong_user.cong_role = list(cong_user.cong_role or []) + ['admin']
        cong_user.save()

        # Return the full cong_settings — the client iterates
        # result.cong_settings.midweek_meeting / weekend_meeting immediately.
        return JsonResponse({
            'message': 'CONG_CREATED',
            'cong_id': cong.cong_id,
            'cong_settings': cong.get_settings_for_user(cong_user),
        })


class CongUsersView(View):
    """GET/POST /api/v3/congregations/admin/<id>/users

    Also serves the legacy GET /members alias (same response).
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        # SHAPE FIX: frontend (apiCongregationUsersGet / useAllUsers) expects a
        # bare CongregationUserType[] array, NOT {users:[...]}. A wrapped object
        # fails its Array.isArray(data) guard and silently drops every user.
        cong = cong_user.congregation
        current_visitor = verify_visitor_cookie(request)
        return JsonResponse(_users_list(cong, current_visitor), safe=False)

    def post(self, request, id):
        """Add a user to the congregation (VIP / baptized flow).

        Body (apiCreateUser): cong_person_uid, cong_role, user_firstname,
        user_lastname, user_id. user_id, when present, is an existing global
        account found via /users/global — we attach it to this congregation.
        Returns the full CongregationUserType[] list after the change.
        """
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from api.models import CongUser

        cong = cong_user.congregation
        target_user_id = body.get('user_id', '') or ''
        cong_person_uid = body.get('cong_person_uid', '') or ''
        cong_role = body.get('cong_role') or ['publisher']
        firstname = body.get('user_firstname', '') or ''
        lastname = body.get('user_lastname', '') or ''
        email = body.get('email', '') or ''  # optional legacy fallback

        member = None
        if target_user_id:
            try:
                member = CongUser.objects.filter(id=target_user_id).first()
            except Exception:
                member = None

        if member is None and email:
            auth_user, _ = User.objects.get_or_create(
                username=email, defaults={'email': email}
            )
            member = CongUser.objects.filter(auth_user=auth_user).first()
            if member is None:
                member = CongUser(auth_user=auth_user)

        if member is None:
            member = CongUser()

        member.congregation = cong
        member.firstname = name_field(firstname)
        member.lastname = name_field(lastname)
        member.user_local_uid = cong_person_uid
        member.cong_role = cong_role
        member.save()

        current_visitor = verify_visitor_cookie(request)
        return JsonResponse(_users_list(cong, current_visitor), safe=False)


class CongUserGlobalSearchView(View):
    """GET /api/v3/congregations/admin/<id>/users/global?email=<email>

    Used by person_select to link an existing account to a congregation role.
    The CALLER (usePersonSelect) reads `data.id` straight off the response
    body, so we return the matched user object {id, profile} directly (200) or
    404 when none matches — NOT a {status,data:[...]} wrapper, which would make
    data.id undefined and break the linking flow.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        from api.models import CongUser

        email = request.GET.get('email', '') or ''
        if not email:
            return JsonResponse({'message': 'ACCOUNT_NOT_FOUND'}, status=404)

        member = (
            CongUser.objects
            .filter(auth_user__email__icontains=email)
            .select_related('auth_user')
            .first()
        )
        if member is None:
            return JsonResponse({'message': 'ACCOUNT_NOT_FOUND'}, status=404)

        user = _serialize_user(member)
        # Single object so the caller's data.id resolves; profile only.
        return JsonResponse({'id': user['id'], 'profile': user['profile']})


class CongUserDetailView(View):
    """PATCH/DELETE /api/v3/congregations/admin/<id>/users/<user_id>"""

    def patch(self, request, id, user_id):
        """Update a user. Returns the full CongregationUserType[] list."""
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        cong = cong_user.congregation
        member = _get_member(cong, user_id)
        if member is not None:
            # cong_person_uid -> user_local_uid,
            # cong_person_delegates -> user_members_delegate,
            # first_name/last_name -> firstname/lastname.
            # user_secret_code is accepted and ignored (placeholder).
            if 'cong_person_uid' in body:
                member.user_local_uid = body.get('cong_person_uid', '') or ''
            if 'cong_role' in body:
                member.cong_role = body.get('cong_role') or []
            if 'cong_person_delegates' in body:
                member.user_members_delegate = body.get('cong_person_delegates') or []
            if 'first_name' in body:
                member.firstname = name_field(body.get('first_name', ''))
            if 'last_name' in body:
                member.lastname = name_field(body.get('last_name', ''))
            member.save()

        current_visitor = verify_visitor_cookie(request)
        return JsonResponse(_users_list(cong, current_visitor), safe=False)

    def delete(self, request, id, user_id):
        """Remove a user from this congregation (keep the auth_user account).

        Returns the remaining CongregationUserType[] list.
        """
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        member = _get_member(cong, user_id)
        if member is not None:
            # Deleting the CongUser does not cascade to auth_user (the cascade
            # runs auth_user -> CongUser, not the reverse).
            member.delete()

        current_visitor = verify_visitor_cookie(request)
        return JsonResponse(_users_list(cong, current_visitor), safe=False)


class CongUserSessionsView(View):
    """DELETE /api/v3/congregations/admin/<id>/users/<user_id>/sessions

    Revoke one admin-side session by identifier. Returns the full
    CongregationUserType[] list with the user's sessions updated.
    """

    def delete(self, request, id, user_id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        identifier = body.get('identifier', '') or ''
        cong = cong_user.congregation
        member = _get_member(cong, user_id)
        if member is not None:
            member.sessions = [
                s for s in (member.sessions or [])
                if s.get('identifier') != identifier
            ]
            member.save(update_fields=['sessions'])

        current_visitor = verify_visitor_cookie(request)
        return JsonResponse(_users_list(cong, current_visitor), safe=False)


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

        # SHAPE FIX: frontend (apiPocketUserCreate) sends user_firstname,
        # user_lastname, user_secret_code, cong_person_uid, cong_role — and
        # expects the full CongregationUserType[] list back (not {message,id}).
        cong = cong_user.congregation
        firstname = name_field(body.get('user_firstname', ''))
        lastname = name_field(body.get('user_lastname', ''))
        cong_person_uid = body.get('cong_person_uid', '') or ''
        cong_role = body.get('cong_role') or ['publisher']
        secret = body.get('user_secret_code', '') or ''

        # The client computes+shows the plaintext code locally and sends the
        # encrypted secret; the invitation_code panel decrypts
        # profile.pocket_invitation_code, so store the secret as-is. Fall back
        # to a generated code if none was supplied.
        code = secret or (
            f"{cong.cong_prefix or 'LC'}-"
            f"{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"
        )

        CongUser.objects.create(
            congregation=cong,
            firstname=firstname,
            lastname=lastname,
            user_local_uid=cong_person_uid,
            cong_role=cong_role,
            pocket_invitation_code=code,
        )

        current_visitor = verify_visitor_cookie(request)
        return JsonResponse(_users_list(cong, current_visitor), safe=False)


class PocketUserDeleteView(View):
    """DELETE /api/v3/congregations/admin/<id>/pocket-user/<user_id>

    Clear a pocket user's invitation code (keep the CongUser record).
    Returns the full CongregationUserType[] list.
    """

    def delete(self, request, id, user_id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        member = _get_member(cong, user_id)
        if member is not None:
            member.pocket_invitation_code = None
            member.save(update_fields=['pocket_invitation_code'])

        current_visitor = verify_visitor_cookie(request)
        return JsonResponse(_users_list(cong, current_visitor), safe=False)


class CongregationEraseView(View):
    """DELETE /api/v3/congregations/admin/<id>/erase

    Destructive: cascade-delete the caller's congregation and ALL of its data
    (members, backup tables, metadata — every FK is on_delete=CASCADE). The
    client already gates this behind an admin action that requires re-entering
    the master key, then signs out and wipes its local database on the 200.
    """

    def delete(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        # Body carries {key} (the master key). It is client-encrypted and the
        # server keeps encryption pass-through (see encryption policy), so the
        # admin-role check + the client's own master-key prompt are the gate.
        try:
            json.loads(request.body)
        except Exception:
            pass

        cong = cong_user.congregation
        if cong is not None:
            cong_name = cong.cong_name
            cong.delete()  # cascades to members / backup_tables / metadata
            logger.warning("Congregation erased by admin: %s", cong_name)

        return JsonResponse({'message': 'CONGREGATION_DELETED'})


def _serialize_join_requests(cong):
    """Stored join requests -> APIUserRequest[] {user, request_date, first/last}."""
    data = cong.data if isinstance(cong.data, dict) else {}
    out = []
    for r in (data.get('join_requests') or []):
        if not isinstance(r, dict):
            continue
        out.append({
            'user': r.get('user_id', ''),
            'request_date': r.get('requested_at', ''),
            'firstname': r.get('firstname', ''),
            'lastname': r.get('lastname', ''),
        })
    return out


def _drop_join_request(cong, user_id):
    """Remove a join request by user_id and persist."""
    data = cong.data if isinstance(cong.data, dict) else {}
    reqs = [
        r for r in (data.get('join_requests') or [])
        if isinstance(r, dict) and r.get('user_id') != user_id
    ]
    data['join_requests'] = reqs
    cong.data = data
    cong.save(update_fields=['data'])


class JoinRequestsView(View):
    """DELETE/PATCH /api/v3/congregations/admin/<id>/join-requests

    Requests are recorded by UserJoinCongregationView in Congregation.data
    ['join_requests']. DELETE declines (drops the request); PATCH accepts
    (attaches the user to this congregation with the chosen role, then drops
    the request). Both return the remaining APIUserRequest[].
    """

    def delete(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        target = request.META.get('HTTP_USER', '') or ''
        if target:
            _drop_join_request(cong, target)
        return JsonResponse(_serialize_join_requests(cong), safe=False)

    def patch(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        from api.models import CongUser

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            body = {}

        cong = cong_user.congregation
        target = request.META.get('HTTP_USER', '') or ''
        role = body.get('role') or ['publisher']
        person_uid = body.get('person_uid', '') or ''

        if target:
            # Attach the requesting user to this congregation.
            member = CongUser.objects.filter(id=target).first()
            if member is not None:
                member.congregation = cong
                member.cong_role = role
                if person_uid:
                    member.user_local_uid = person_uid
                if body.get('firstname'):
                    member.firstname = name_field(body.get('firstname'))
                if body.get('lastname'):
                    member.lastname = name_field(body.get('lastname'))
                member.save()
            _drop_join_request(cong, target)

        return JsonResponse(_serialize_join_requests(cong), safe=False)


class CongregationApplicationView(View):
    """PATCH/DELETE /api/v3/congregations/<id>/applications/<app_id>

    Member-level. Applications live in CongBackupTable(table_name='applications')
    as a list keyed by request_id. Returns the APRecordType[] array.
    """

    def patch(self, request, id, app_id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        from api.models import CongBackupTable

        application = body.get('application') or {}
        request_id = application.get('request_id') or app_id

        cong = cong_user.congregation
        bt, _ = CongBackupTable.objects.get_or_create(
            congregation=cong, table_name='applications',
            defaults={'data': []}
        )
        records = bt.data if isinstance(bt.data, list) else []

        replaced = False
        for i, rec in enumerate(records):
            if isinstance(rec, dict) and rec.get('request_id') == request_id:
                records[i] = application
                replaced = True
                break
        if not replaced:
            records.append(application)

        bt.data = records
        bt.save()

        return JsonResponse(records, safe=False)

    def delete(self, request, id, app_id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        from api.models import CongBackupTable

        cong = cong_user.congregation
        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='applications'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse([], safe=False)

        records = bt.data if isinstance(bt.data, list) else []
        records = [
            rec for rec in records
            if not (isinstance(rec, dict) and rec.get('request_id') == app_id)
        ]
        bt.data = records
        bt.save()

        return JsonResponse(records, safe=False)


class MasterKeyView(View):
    """GET/POST /api/v3/congregations/admin/<id>/master-key"""

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        settings = cong.cong_settings or {}
        # APIResponseMessageString {status, message}; message is the stored
        # (encrypted) master key, which the client decrypts.
        return JsonResponse({
            'status': 200,
            'message': settings.get('cong_master_key', '') or '',
        })

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
    """GET/POST /api/v3/congregations/admin/<id>/access-code"""

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user or 'admin' not in (cong_user.cong_role or []):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        settings = cong.cong_settings or {}
        # APIResponseMessageString {status, message}; message is the stored
        # (encrypted) access code, which the client decrypts.
        return JsonResponse({
            'status': 200,
            'message': settings.get('cong_access_code', '') or '',
        })

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


class LocalUidView(View):
    """POST /api/v3/congregations/admin/<id>/local-uid

    Link the requesting user to the person record that represents them in the
    congregation (sets CongUser.user_local_uid). Any authenticated member may
    set their own — it is not admin-only — so the path <id> is informational.
    Without this, the dashboard's person-record step 404s and handleSavePerson
    fails trying to parse the HTML 404 as JSON.
    """

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        user_uid = body.get('user_uid', '') or ''
        cong_user.user_local_uid = user_uid
        cong_user.save(update_fields=['user_local_uid'])

        return JsonResponse(
            {'message': 'LOCAL_UID_UPDATED', 'user_local_uid': user_uid}
        )


class CountriesView(View):
    """GET /api/v3/congregations/countries (public, no auth)

    Frontend (apiFetchCountries / useCountry) expects a bare
    CountryResponseType[] array — it runs Array.isArray(data) and drops a
    wrapped object. The optional ?language= param is accepted and ignored.
    """

    def get(self, request):
        from api.services.countries import COUNTRIES

        return JsonResponse([
            {
                'countryCode': code,
                'countryName': name,
                'countryGuid': code,
            }
            for code, name in COUNTRIES
        ], safe=False)


class SearchCongregationsView(View):
    """GET /api/v3/congregations/search (public, no auth)

    Frontend (apiFetchCongregations / useCongregation) calls with
    ?language=&country=&name= and expects a bare CongregationResponseType[]
    array (Array.isArray guard). Filter by name (the real param the client
    sends) and, when provided, country.
    """

    def get(self, request):
        from api.models import Congregation

        # Client sends `name`; accept legacy `q` as a fallback.
        name = request.GET.get('name', '') or request.GET.get('q', '') or ''
        country = request.GET.get('country', '') or ''

        congs = Congregation.objects.all()
        if name:
            congs = congs.filter(cong_name__icontains=name)
        if country:
            congs = congs.filter(country_code__iexact=country)
        congs = congs[:10]

        return JsonResponse([
            {
                'congGuid': c.cong_id,
                'congName': c.cong_name,
                'language': '',
                'address': '',
                'circuit': '',
                'location': {'lat': 0, 'lng': 0},
                'midweekMeetingTime': {'weekday': 0, 'time': ''},
                'weekendMeetingTime': {'weekday': 0, 'time': ''},
            }
            for c in congs
        ], safe=False)
