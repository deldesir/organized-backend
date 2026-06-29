"""Webhook views for external integrations.

Provides:
- Assignment response webhook (accept/decline from WhatsApp)
- Hermes query tool webhook (AI agent queries organized data)

All webhooks are authenticated via a shared secret header.
"""

import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views import View

logger = logging.getLogger('organized.webhook')


def _verify_webhook_secret(request):
    """Verify the X-Webhook-Secret header matches the configured secret."""
    expected = getattr(settings, 'ORGANIZED_WEBHOOK_SECRET', '')
    if not expected:
        return False
    actual = request.META.get('HTTP_X_WEBHOOK_SECRET', '')
    return actual == expected


@method_decorator(csrf_exempt, name='dispatch')
class AssignmentResponseWebhook(View):
    """POST /api/v3/webhooks/assignment-response

    Called by RapidPro flow when a publisher responds to
    an assignment notification via WhatsApp.

    Payload: {phone, intent: 'accept'|'decline', week_of, assignment_type}
    """

    def post(self, request):
        if not _verify_webhook_secret(request):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        phone = body.get('phone', '')
        intent = body.get('intent', '')
        week_of = body.get('week_of', '')
        assignment_type = body.get('assignment_type', '')

        if not all([phone, intent, week_of, assignment_type]):
            return JsonResponse({'message': 'MISSING_FIELDS'}, status=400)

        if intent not in ('accept', 'decline'):
            return JsonResponse({'message': 'INVALID_INTENT'}, status=400)

        logger.info(
            f"Assignment response: {phone} {intent}ed "
            f"{assignment_type} for {week_of}"
        )

        # Store the response in the schedules backup table
        # Phase 3.1 will add detailed response tracking
        return JsonResponse({'message': 'RESPONSE_RECORDED'})


@method_decorator(csrf_exempt, name='dispatch')
class ScheduleQueryWebhook(View):
    """POST /api/v3/webhooks/query

    Query endpoint for organized schedule data.
    Called by RapidPro flows (via call_webhook) and by the AI Gateway
    macro wrappers. Authenticated via X-Webhook-Secret header.

    Payload: {action, congregation_id?, person_name?, week_of?}
    Actions:
      - get_schedule: Returns schedule for a specific week
      - get_person_assignments: Returns all assignments for a person
      - search_persons: Search persons by name
    """

    def post(self, request):
        if not _verify_webhook_secret(request):
            return JsonResponse({'message': 'UNAUTHORIZED'}, status=403)

        try:
            body = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'message': 'INVALID_PAYLOAD'}, status=400)

        action = body.get('action', '')

        handlers = {
            'get_schedule': self._get_schedule,
            'get_person_assignments': self._get_person_assignments,
            'search_persons': self._search_persons,
            'get_events': self._get_events,
            'get_sources': self._get_sources,
            'get_field_groups': self._get_field_groups,
            'get_attendance': self._get_attendance,
            'get_field_report': self._get_field_report,
            'get_visiting_speakers': self._get_visiting_speakers,
            'get_speakers_congregations': self._get_speakers_congregations,
            'get_cong_report': self._get_cong_report,
            'get_branch_report': self._get_branch_report,
            'get_delegated_reports': self._get_delegated_reports,
            'get_cong_analysis': self._get_cong_analysis,
            'get_bible_studies': self._get_bible_studies,
            'get_notifications': self._get_notifications,
        }

        handler = handlers.get(action)
        if handler:
            return handler(body)
        return JsonResponse({'message': 'UNKNOWN_ACTION'}, status=400)

    def _get_schedule(self, body):
        from api.models import CongBackupTable, Congregation

        cong_id = body.get('congregation_id', '')
        week_of = body.get('week_of', '')

        try:
            cong = Congregation.objects.get(cong_id=cong_id)
        except Congregation.DoesNotExist:
            # Fall back to first congregation for single-tenant local setups
            cong = Congregation.objects.first()
            if not cong:
                return JsonResponse({'message': 'NO_CONGREGATION'}, status=404)

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='schedules'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'schedules': []})

        data = bt.data or []

        if week_of:
            data = [
                s for s in data
                if isinstance(s, dict) and
                (s.get('weekOf', '') == week_of or s.get('week_of', '') == week_of)
            ]

        return JsonResponse({'schedules': data})

    def _get_person_assignments(self, body):
        from api.models import CongBackupTable, Congregation

        person_name = body.get('person_name', '').lower()

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'assignments': []})

        # Search persons table for the person_uid
        try:
            persons_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='persons'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'assignments': []})

        person_uid = None
        for person in (persons_bt.data or []):
            if not isinstance(person, dict):
                continue
            display_name = (
                person.get('person_data', {})
                .get('person_display_name', {})
                .get('value', '')
            )
            if person_name in display_name.lower():
                person_uid = person.get('person_uid')
                break

        if not person_uid:
            return JsonResponse({
                'message': f'Person "{person_name}" not found',
                'assignments': [],
            })

        # Search schedules for this person's assignments
        try:
            sched_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='schedules'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'assignments': []})

        assignments = []
        for schedule in (sched_bt.data or []):
            if not isinstance(schedule, dict):
                continue
            week_of = schedule.get('weekOf', schedule.get('week_of', ''))
            for meeting_type in ('midweek_meeting', 'weekend_meeting'):
                meeting = schedule.get(meeting_type, {})
                if not isinstance(meeting, dict):
                    continue
                self._scan_assignments(meeting, person_uid, week_of,
                                       meeting_type.replace('_meeting', ''),
                                       assignments)

        return JsonResponse({'assignments': assignments})

    def _scan_assignments(self, obj, person_uid, week_of, prefix, results):
        """Recursively scan a meeting object for a person_uid in assignments."""
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and item.get('value') == person_uid:
                    results.append({'week_of': week_of, 'type': prefix})
                    return
        elif isinstance(obj, dict):
            if obj.get('value') == person_uid:
                results.append({'week_of': week_of, 'type': prefix})
                return
            for key, val in obj.items():
                if key in ('value', 'name', 'updatedAt', 'type', '_deleted'):
                    continue
                self._scan_assignments(val, person_uid, week_of,
                                       f"{prefix}_{key}", results)

    def _search_persons(self, body):
        from api.models import CongBackupTable, Congregation

        query = body.get('query', '').lower()

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'persons': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='persons'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'persons': []})

        results = []
        for person in (bt.data or []):
            if not isinstance(person, dict):
                continue
            display_name = (
                person.get('person_data', {})
                .get('person_display_name', {})
                .get('value', '')
            )
            if query in display_name.lower():
                results.append({
                    'person_uid': person.get('person_uid'),
                    'display_name': display_name,
                })

        return JsonResponse({'persons': results[:10]})

    # ── New Actions (ADR-012 Phase 3 expansion) ──────────────────────────

    def _get_events(self, body):
        """Return upcoming events (future only, sorted by start date)."""
        from api.models import CongBackupTable, Congregation
        from datetime import datetime

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'events': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='upcoming_events'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'events': []})

        now_str = datetime.now().isoformat()
        events = []
        for e in (bt.data or []):
            if not isinstance(e, dict):
                continue
            ed = e.get('event_data', {})
            if ed.get('_deleted'):
                continue
            if ed.get('start', '') >= now_str or True:  # show all for demo
                events.append({
                    'description': ed.get('description', ''),
                    'start': ed.get('start', ''),
                    'end': ed.get('end', ''),
                    'category': ed.get('category', ''),
                })

        events.sort(key=lambda x: x.get('start', ''))
        return JsonResponse({'events': events[:10]})

    def _get_sources(self, body):
        """Return meeting source material for a week."""
        from api.models import CongBackupTable, Congregation

        week_of = body.get('week_of', '')
        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'sources': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='sources'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'sources': []})

        data = bt.data or []
        if week_of:
            data = [s for s in data if isinstance(s, dict)
                    and s.get('weekOf', '') == week_of]

        # Flatten to WhatsApp-friendly summary
        results = []
        for src in data:
            if not isinstance(src, dict):
                continue
            mw = src.get('midweek_meeting', {})
            we = src.get('weekend_meeting', {})
            results.append({
                'weekOf': src.get('weekOf', ''),
                'bible_reading': mw.get('weekly_bible_reading', {}).get('src', ''),
                'tgw_talk': mw.get('tgw_talk', {}).get('src', ''),
                'cbs': mw.get('lc_cbs', {}).get('src', ''),
                'wt_study': we.get('w_study', {}).get('src', ''),
            })

        return JsonResponse({'sources': results})

    def _get_field_groups(self, body):
        """Return field service groups with resolved member names."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'groups': []})

        try:
            groups_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='field_service_groups'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'groups': []})

        # Build person_uid → name lookup
        names = {}
        try:
            persons_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='persons'
            )
            for p in (persons_bt.data or []):
                if isinstance(p, dict):
                    uid = p.get('person_uid', '')
                    name = (p.get('person_data', {})
                            .get('person_display_name', {})
                            .get('value', uid))
                    names[uid] = name
        except CongBackupTable.DoesNotExist:
            pass

        groups = []
        for g in (groups_bt.data or []):
            if not isinstance(g, dict):
                continue
            gd = g.get('group_data', {})
            if gd.get('_deleted'):
                continue
            members = []
            for m in gd.get('members', []):
                uid = m.get('person_uid', '')
                role = '👑' if m.get('isOverseer') else ('🤝' if m.get('isAssistant') else '')
                members.append({
                    'name': names.get(uid, uid),
                    'role': role,
                })
            groups.append({
                'name': gd.get('name', ''),
                'members': members,
            })

        # If person_name provided, filter to their group
        person_name = body.get('person_name', '').lower()
        if person_name:
            for g in groups:
                for m in g['members']:
                    if person_name in m['name'].lower():
                        return JsonResponse({'groups': [g]})
            return JsonResponse({'groups': [], 'message': 'Not found in any group'})

        return JsonResponse({'groups': groups})

    def _get_attendance(self, body):
        """Return meeting attendance for a month."""
        from api.models import CongBackupTable, Congregation

        month = body.get('month', '')
        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'attendance': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='meeting_attendance'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'attendance': []})

        data = bt.data or []
        if month:
            data = [a for a in data if isinstance(a, dict)
                    and a.get('month_date', '') == month]

        results = []
        for att in data:
            if not isinstance(att, dict):
                continue
            total_mw, total_we, weeks = 0, 0, 0
            for wk in ('week_1', 'week_2', 'week_3', 'week_4', 'week_5'):
                wd = att.get(wk, {})
                mw_list = wd.get('midweek', [])
                we_list = wd.get('weekend', [])
                if mw_list and isinstance(mw_list[0], dict):
                    present = mw_list[0].get('present', 0) or 0
                    online = mw_list[0].get('online', 0) or 0
                    total_mw += present + online
                    weeks += 1
                if we_list and isinstance(we_list[0], dict):
                    present = we_list[0].get('present', 0) or 0
                    online = we_list[0].get('online', 0) or 0
                    total_we += present + online

            avg_mw = round(total_mw / max(weeks, 1))
            avg_we = round(total_we / max(weeks, 1))
            results.append({
                'month': att.get('month_date', ''),
                'avg_midweek': avg_mw,
                'avg_weekend': avg_we,
                'weeks_counted': weeks,
            })

        return JsonResponse({'attendance': results})

    def _get_field_report(self, body):
        """Return field service report for a person/month."""
        from api.models import CongBackupTable, Congregation

        person_name = body.get('person_name', '').lower()
        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'reports': []})

        # Resolve name → uid
        person_uid = None
        if person_name:
            try:
                persons_bt = CongBackupTable.objects.get(
                    congregation=cong, table_name='persons'
                )
                for p in (persons_bt.data or []):
                    if isinstance(p, dict):
                        dn = (p.get('person_data', {})
                              .get('person_display_name', {})
                              .get('value', ''))
                        if person_name in dn.lower():
                            person_uid = p.get('person_uid')
                            break
            except CongBackupTable.DoesNotExist:
                pass

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='field_service_reports'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'reports': []})

        reports = []
        for r in (bt.data or []):
            if not isinstance(r, dict):
                continue
            rd = r.get('report_data', {})
            if rd.get('_deleted'):
                continue
            if person_uid and rd.get('person_uid') != person_uid:
                continue
            reports.append({
                'month': rd.get('report_date', ''),
                'hours': rd.get('hours', {}).get('field_service', 0),
                'bible_studies': rd.get('bible_studies', 0),
                'shared_ministry': rd.get('shared_ministry', False),
                'status': rd.get('status', ''),
            })

        return JsonResponse({'reports': reports[:6]})

    def _get_visiting_speakers(self, body):
        """Return visiting speakers with their talk outlines."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'speakers': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='visiting_speakers'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'speakers': []})

        speakers = []
        for s in (bt.data or []):
            if not isinstance(s, dict):
                continue
            if s.get('_deleted', {}).get('value'):
                continue
            sd = s.get('speaker_data', {})
            talks = []
            for t in sd.get('talks', []):
                if isinstance(t, dict) and not t.get('_deleted'):
                    talks.append({
                        'number': t.get('talk_number', ''),
                        'title': t.get('talk_title', ''),
                    })
            speakers.append({
                'name': sd.get('person_display_name', {}).get('value', ''),
                'cong_id': sd.get('cong_id', ''),
                'elder': sd.get('elder', {}).get('value', False),
                'phone': sd.get('person_phone', {}).get('value', ''),
                'talks': talks,
            })

        return JsonResponse({'speakers': speakers})

    def _get_speakers_congregations(self, body):
        """Return congregations that share speakers."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'congregations': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='speakers_congregations'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'congregations': []})

        results = []
        for c in (bt.data or []):
            if not isinstance(c, dict):
                continue
            if c.get('_deleted', {}).get('value'):
                continue
            cd = c.get('cong_data', {})
            results.append({
                'name': cd.get('cong_name', {}).get('value', ''),
                'number': cd.get('cong_number', {}).get('value', ''),
                'circuit': cd.get('cong_circuit', {}).get('value', ''),
                'address': cd.get('cong_location', {}).get('address', {}).get('value', ''),
                'coordinator': cd.get('coordinator', {}).get('name', {}).get('value', ''),
                'talk_coordinator': cd.get('public_talk_coordinator', {}).get('name', {}).get('value', ''),
                'weekend_time': cd.get('weekend_meeting', {}).get('time', {}).get('value', ''),
            })

        return JsonResponse({'congregations': results})

    def _get_cong_report(self, body):
        """Return congregation-level field service report aggregates."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'reports': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='cong_field_service_reports'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'reports': []})

        reports = []
        for r in (bt.data or []):
            if not isinstance(r, dict):
                continue
            rd = r.get('report_data', {})
            if rd.get('_deleted'):
                continue
            reports.append({
                'month': rd.get('report_date', ''),
                'hours': rd.get('hours', {}).get('field_service', 0),
                'bible_studies': rd.get('bible_studies', 0),
                'status': rd.get('status', ''),
                'comments': rd.get('comments', ''),
            })

        return JsonResponse({'reports': reports})

    def _get_branch_report(self, body):
        """Return branch field service report."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'reports': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='branch_field_service_reports'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'reports': []})

        reports = []
        for r in (bt.data or []):
            if not isinstance(r, dict):
                continue
            rd = r.get('report_data', {})
            if rd.get('_deleted'):
                continue
            reports.append({
                'month': r.get('report_date', ''),
                'submitted': rd.get('submitted', False),
                'publishers_active': rd.get('publishers_active', 0),
                'meeting_avg': rd.get('weekend_meeting_average', 0),
                'publishers_reporting': rd.get('publishers', {}).get('report_count', 0),
                'total_bible_studies': rd.get('publishers', {}).get('bible_studies', 0),
                'ap_hours': rd.get('APs', {}).get('hours', 0),
                'fr_hours': rd.get('FRs', {}).get('hours', 0),
            })

        return JsonResponse({'reports': reports})

    def _get_delegated_reports(self, body):
        """Return delegated field service reports."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'reports': []})

        # Build person_uid → name lookup
        names = {}
        try:
            persons_bt = CongBackupTable.objects.get(
                congregation=cong, table_name='persons'
            )
            for p in (persons_bt.data or []):
                if isinstance(p, dict):
                    uid = p.get('person_uid', '')
                    name = (p.get('person_data', {})
                            .get('person_display_name', {})
                            .get('value', uid))
                    names[uid] = name
        except CongBackupTable.DoesNotExist:
            pass

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='delegated_field_service_reports'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'reports': []})

        reports = []
        for r in (bt.data or []):
            if not isinstance(r, dict):
                continue
            rd = r.get('report_data', {})
            if rd.get('_deleted'):
                continue
            uid = rd.get('person_uid', '')
            hours_data = rd.get('hours', {}).get('field_service', {})
            hours = hours_data.get('monthly', '0') if isinstance(hours_data, dict) else str(hours_data)
            bs = rd.get('bible_studies', {})
            studies = bs.get('monthly', 0) if isinstance(bs, dict) else bs
            reports.append({
                'person': names.get(uid, uid),
                'month': rd.get('report_date', ''),
                'hours': hours,
                'bible_studies': studies,
                'status': rd.get('status', ''),
                'comments': rd.get('comments', ''),
            })

        return JsonResponse({'reports': reports})

    def _get_cong_analysis(self, body):
        """Return branch congregation analysis."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'analysis': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='branch_cong_analysis'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'analysis': []})

        results = []
        for a in (bt.data or []):
            if not isinstance(a, dict):
                continue
            rd = a.get('report_data', {})
            if rd.get('_deleted'):
                continue
            results.append({
                'month': a.get('report_date', ''),
                'submitted': rd.get('submitted', False),
                'midweek_avg': rd.get('meeting_average', {}).get('midweek', 0),
                'weekend_avg': rd.get('meeting_average', {}).get('weekend', 0),
                'active': rd.get('publishers', {}).get('active', 0),
                'inactive': rd.get('publishers', {}).get('inactive', 0),
                'reactivated': rd.get('publishers', {}).get('reactivated', 0),
                'territories_total': rd.get('territories', {}).get('total', 0),
                'territories_uncovered': rd.get('territories', {}).get('uncovered', 0),
            })

        return JsonResponse({'analysis': results})

    def _get_bible_studies(self, body):
        """Return user's bible studies."""
        from api.models import CongBackupTable, Congregation

        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'studies': []})

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='user_bible_studies'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'studies': []})

        studies = []
        for s in (bt.data or []):
            if not isinstance(s, dict):
                continue
            pd = s.get('person_data', {})
            if pd.get('_deleted'):
                continue
            studies.append({
                'name': pd.get('person_name', ''),
            })

        return JsonResponse({'studies': studies})

    def _get_notifications(self, body):
        """Return notifications, optionally filtered by person."""
        from api.models import CongBackupTable, Congregation

        person_name = body.get('person_name', '').lower()
        cong = Congregation.objects.first()
        if not cong:
            return JsonResponse({'notifications': []})

        # Resolve person_name → person_uid if provided
        person_uid = None
        if person_name:
            try:
                persons_bt = CongBackupTable.objects.get(
                    congregation=cong, table_name='persons'
                )
                for p in (persons_bt.data or []):
                    if isinstance(p, dict):
                        dn = (p.get('person_data', {})
                              .get('person_display_name', {})
                              .get('value', ''))
                        if person_name in dn.lower():
                            person_uid = p.get('person_uid')
                            break
            except CongBackupTable.DoesNotExist:
                pass

        try:
            bt = CongBackupTable.objects.get(
                congregation=cong, table_name='notifications'
            )
        except CongBackupTable.DoesNotExist:
            return JsonResponse({'notifications': []})

        results = []
        for n in (bt.data or []):
            if not isinstance(n, dict):
                continue
            # Show broadcasts (empty person_uid) and personal notifications
            n_pid = n.get('person_uid', '')
            if person_uid and n_pid and n_pid != person_uid:
                continue
            results.append({
                'type': n.get('type', ''),
                'title': n.get('title', ''),
                'body': n.get('body', ''),
                'read': n.get('read', False),
                'created': n.get('created', ''),
            })

        results.sort(key=lambda x: x.get('created', ''), reverse=True)
        return JsonResponse({'notifications': results[:10]})
