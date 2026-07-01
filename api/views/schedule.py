"""Meeting schedule views.

Serves published meeting schedules to pocket and VIP users, and provides
the schedule-editor read/publish endpoints used by the app's
schedule_publish feature.

Mirrors sws2apps-api's congregation meeting-schedule controllers. Auth is
the project-standard session-cookie model: the VisitorCheckerMiddleware sets
request.cong_user from the signed visitorid cookie before the view runs.
See api/views/congregation.py (MasterKeyView/AccessCodeView) for the pattern
and api/services/backup.py for the authoritative field names/shapes.
"""

import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views import View

from api.services.roles import get_user_roles

logger = logging.getLogger('organized.schedule')


def _get_table_data(cong, table_name):
    """Return CongBackupTable.data for table_name, or None if it doesn't exist."""
    from api.models import CongBackupTable

    try:
        return CongBackupTable.objects.get(
            congregation=cong, table_name=table_name
        ).data
    except CongBackupTable.DoesNotExist:
        return None


def _save_table_data(cong, table_name, data):
    """Upsert a congregation-scoped backup table (mirrors backup._save_cong_table)."""
    from api.models import CongBackupTable

    CongBackupTable.objects.update_or_create(
        congregation=cong, table_name=table_name,
        defaults={'data': data},
    )


def _touch_table_metadata(cong, table_name):
    """Bump a congregation table's sync metadata to 'now'.

    Consumers (pocket / person-minimal users) receive a table from the backup
    only when its server metadata differs from the timestamp they last synced
    (see backup._maybe_add_table). Without bumping metadata here, a freshly
    published public_sources/public_schedules table would compare equal ('' ==
    '') for a never-synced consumer and never be delivered — the publish would
    silently reach nobody. Mirrors backup._update_metadata's timestamp write.
    """
    from api.models import Metadata

    Metadata.objects.update_or_create(
        congregation=cong, cong_user=None, key=table_name,
        defaults={'value': timezone.now().isoformat()},
    )


class PocketMeetingScheduleView(View):
    """GET /api/v3/sws-pocket/meeting-schedule

    Pocket-only published-schedule view. No body. Authenticated via the
    session cookie. Returns the minimal/published schedule blob for the
    pocket user's congregation: 'public_schedules' if present, otherwise a
    fallback to 'schedules'. Returns {} when neither table exists yet.
    """

    def get(self, request):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation

        data = _get_table_data(cong, 'public_schedules')
        if data is None:
            data = _get_table_data(cong, 'schedules')
        if data is None:
            return JsonResponse({})

        # data is the raw stored blob (a list of SchedWeekType); safe=False
        # lets a non-dict top level serialize.
        return JsonResponse(data, safe=False)


class CongregationMeetingScheduleView(View):
    """GET /api/v3/congregations/<id>/meeting-schedule

    VIP published-schedule view. Authenticated via the session cookie.
    Returns the published 'public_schedules' blob for the VIP account's
    congregation (published via PublishedSchedulesView, the same table
    pocket/minimal users read). The path <id> is the cong_id; the requesting
    user must belong to that congregation. Returns {} when nothing has been
    published yet.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        if not cong or cong.cong_id != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        data = _get_table_data(cong, 'public_schedules')
        if data is None:
            return JsonResponse({})

        return JsonResponse(data, safe=False)


class PublishedSchedulesView(View):
    """GET/POST /api/v3/congregations/meeting/<id>/schedules

    Schedule-editor read (GET) and publish (POST) endpoint. The path <id> is
    the cong_id; the requesting user must belong to that congregation and be
    a schedule editor or elder (mirrors backup.py lines 110-114). Outgoing
    public talks are only read/written for publicTalkEditor users.
    """

    def get(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        if not cong or cong.cong_id != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        roles = get_user_roles(cong_user.cong_role)
        if not (roles['scheduleEditor'] or roles['elderRole']
                or roles['publicTalkEditor']):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        # Read back the PUBLISHED copy (public_*), not the editor's private
        # working backup. The publish flow (POST) merges this remote copy with
        # local before republishing, and upstream publicSchedulesGet reads the
        # public sources/schedules storage.
        sources = _get_table_data(cong, 'public_sources')
        if sources is None:
            sources = []

        schedules = _get_table_data(cong, 'public_schedules')
        if schedules is None:
            schedules = []

        talks = []
        if roles['publicTalkEditor']:
            outgoing = cong.outgoing_speakers or {}
            talks = outgoing.get('outgoing_talks', []) or []

        return JsonResponse({
            'sources': sources,
            'schedules': schedules,
            'talks': talks,
            'message': 'SCHEDULES_RETRIEVED',
        })

    def post(self, request, id):
        cong_user = request.cong_user
        if not cong_user:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        cong = cong_user.congregation
        if not cong or cong.cong_id != id:
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        roles = get_user_roles(cong_user.cong_role)
        if not (roles['scheduleEditor'] or roles['elderRole']
                or roles['publicTalkEditor']):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        sources = body.get('sources', [])
        schedules = body.get('schedules', [])
        talks = body.get('talks')

        # Capture the previously published schedules for the diff engine before
        # overwriting. Compare public-vs-public so a redacted publish doesn't
        # look like every assignment on the (fuller) working table was deleted.
        old_schedules_data = _get_table_data(cong, 'public_schedules')
        if old_schedules_data is None:
            old_schedules_data = []

        # Publish into the PUBLIC tables (public_sources/public_schedules) that
        # pocket/person-minimal users read from the backup — NOT the editor's
        # private working sources/schedules tables (overwriting those with the
        # redacted, single-meeting-type public subset would corrupt the
        # editor's own backup). Mirrors upstream cong.publishSchedules().
        _save_table_data(cong, 'public_sources', sources)
        _save_table_data(cong, 'public_schedules', schedules)
        _touch_table_metadata(cong, 'public_sources')
        _touch_table_metadata(cong, 'public_schedules')

        # Outgoing talks ride in Congregation.outgoing_speakers and are only
        # editable by public-talk editors.
        if talks is not None and roles['publicTalkEditor']:
            outgoing = cong.outgoing_speakers or {}
            outgoing['outgoing_talks'] = talks
            cong.outgoing_speakers = outgoing
            cong.save(update_fields=['outgoing_speakers'])

        # Fire the schedule diff engine to notify assigned persons of changes
        # (mirrors backup.save_user_backup lines 218-227). Never let a queueing
        # failure break the publish response.
        if schedules is not None:
            try:
                from api.tasks import detect_schedule_changes
                detect_schedule_changes.delay(
                    cong.pk, old_schedules_data, schedules
                )
            except Exception as e:
                logger.warning(f"Failed to queue schedule diff: {e}")

        return JsonResponse({'message': 'SCHEDULES_PUBLISHED'})
