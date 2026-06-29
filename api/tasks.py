"""Schedule diff engine — Celery task.

Compares new 'schedules' backup data against previous version
to detect assignment changes. Triggers notifications for
publishers whose assignments have been added, removed, or modified.
"""

import json
import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger('organized.diff')


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def detect_schedule_changes(self, congregation_id, old_data, new_data):
    """Compare old and new schedule data to find assignment changes.

    Args:
        congregation_id: PK of the Congregation
        old_data: list of schedule records (previous backup)
        new_data: list of schedule records (new backup)

    Returns:
        list of diffs: [{person_uid, assignment_type, week_of, action}]
    """
    try:
        diffs = _compute_diffs(old_data or [], new_data or [])

        if not diffs:
            logger.info(f"Congregation {congregation_id}: no schedule changes")
            return []

        logger.info(
            f"Congregation {congregation_id}: "
            f"{len(diffs)} schedule changes detected"
        )

        # Trigger notifications for each affected person
        for diff in diffs:
            send_assignment_notification.delay(congregation_id, diff)

        return diffs

    except Exception as exc:
        logger.error(f"Schedule diff failed for cong {congregation_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_assignment_notification(self, congregation_id, diff):
    """Send a notification for a single assignment change.

    Dual-path delivery:
    1. RapidPro flow (if RAPIDPRO_API_TOKEN is set)
    2. Direct WuzAPI message (fallback)
    """
    from api.models import Congregation, Publisher

    try:
        cong = Congregation.objects.get(pk=congregation_id)
    except Congregation.DoesNotExist:
        logger.error(f"Congregation {congregation_id} not found")
        return

    person_uid = diff.get('person_uid')
    if not person_uid:
        return

    # Resolve phone number
    phone = _resolve_phone(cong, person_uid)
    if not phone:
        logger.warning(f"No phone for {person_uid} in {cong.cong_name}")
        return

    # Normalize phone
    phone = normalize_phone(phone)

    action = diff.get('action', 'assigned')
    assignment_type = diff.get('assignment_type', 'Unknown')
    week_of = diff.get('week_of', '')

    message = _format_notification(
        cong.cong_name, action, assignment_type, week_of
    )

    # Path 1: RapidPro flow
    rapidpro_token = getattr(settings, 'RAPIDPRO_API_TOKEN', '')
    flow_uuid = getattr(settings, 'ORGANIZED_NOTIFICATION_FLOW_UUID', '')

    if rapidpro_token and flow_uuid:
        try:
            _send_via_rapidpro(phone, flow_uuid, diff, rapidpro_token)
            logger.info(f"RapidPro notification sent to {phone}")
            return
        except Exception as e:
            logger.warning(f"RapidPro send failed: {e}, falling back to WuzAPI")

    # Path 2: Direct WuzAPI
    try:
        _send_via_wuzapi(phone, message)
        logger.info(f"WuzAPI notification sent to {phone}")
    except Exception as e:
        logger.error(f"WuzAPI send failed: {e}")
        raise self.retry(exc=e)


@shared_task
def check_attendance_anomalies(congregation_id):
    """Weekly task to check for attendance anomalies.

    Runs via Celery beat. Checks meeting_attendance table for
    declining trends and missing reports.
    """
    from api.models import CongBackupTable

    try:
        bt = CongBackupTable.objects.get(
            congregation_id=congregation_id,
            table_name='meeting_attendance'
        )
    except CongBackupTable.DoesNotExist:
        return

    data = bt.data or []
    if len(data) < 4:
        return  # Need at least 4 weeks of data

    # Simple declining trend: average of last 4 weeks vs previous 4
    recent = data[-4:]
    previous = data[-8:-4] if len(data) >= 8 else []

    if not previous:
        return

    def avg_count(records):
        counts = [r.get('count', 0) for r in records if isinstance(r, dict)]
        return sum(counts) / len(counts) if counts else 0

    recent_avg = avg_count(recent)
    prev_avg = avg_count(previous)

    if prev_avg > 0 and (prev_avg - recent_avg) / prev_avg > 0.15:
        logger.warning(
            f"Congregation {congregation_id}: attendance declining "
            f"({prev_avg:.0f} → {recent_avg:.0f}, "
            f"{(prev_avg - recent_avg) / prev_avg * 100:.0f}% drop)"
        )
        # Could trigger a notification to the service overseer here


# --- Private helpers ---

def _compute_diffs(old_schedules, new_schedules):
    """Find assignment differences between old and new schedule data."""
    old_index = _build_assignment_index(old_schedules)
    new_index = _build_assignment_index(new_schedules)

    diffs = []

    # Find new/changed assignments
    for key, new_val in new_index.items():
        old_val = old_index.get(key)
        if old_val is None:
            diffs.append({**new_val, 'action': 'assigned'})
        elif old_val.get('person_uid') != new_val.get('person_uid'):
            # Person changed — notify both old and new
            if old_val.get('person_uid'):
                diffs.append({**old_val, 'action': 'unassigned'})
            if new_val.get('person_uid'):
                diffs.append({**new_val, 'action': 'assigned'})

    # Find removed assignments
    for key, old_val in old_index.items():
        if key not in new_index and old_val.get('person_uid'):
            diffs.append({**old_val, 'action': 'unassigned'})

    return diffs


def _build_assignment_index(schedules):
    """Build a {(week_of, assignment_type): record} index from schedule data."""
    index = {}
    for schedule in schedules:
        if not isinstance(schedule, dict):
            continue
        week_of = schedule.get('weekOf', schedule.get('week_of', ''))
        for key in ('midweek', 'weekend'):
            meeting = schedule.get(key, {})
            if not isinstance(meeting, dict):
                continue
            for part_key, part_val in meeting.items():
                if isinstance(part_val, dict) and 'value' in part_val:
                    assignment_key = (week_of, f"{key}_{part_key}")
                    index[assignment_key] = {
                        'week_of': week_of,
                        'assignment_type': f"{key}_{part_key}",
                        'person_uid': part_val.get('value', ''),
                    }
    return index


def _resolve_phone(congregation, person_uid):
    """Look up a publisher's phone from the persons backup table."""
    from api.models import CongBackupTable

    try:
        bt = CongBackupTable.objects.get(
            congregation=congregation, table_name='persons'
        )
    except CongBackupTable.DoesNotExist:
        return None

    for person in (bt.data or []):
        if not isinstance(person, dict):
            continue
        if person.get('person_uid') == person_uid:
            contacts = person.get('person_data', {}).get('person_contact', {})
            phone = contacts.get('phone', {}).get('value', '')
            return phone or None

    return None


def normalize_phone(phone):
    """Normalize a phone number to E.164 format."""
    phone = phone.strip().replace(' ', '').replace('-', '')
    if not phone.startswith('+'):
        phone = '+' + phone
    return phone


def _format_notification(cong_name, action, assignment_type, week_of):
    """Format a human-readable notification message."""
    readable_type = assignment_type.replace('_', ' ').title()

    if action == 'assigned':
        return (
            f"📋 *{cong_name}*\n\n"
            f"You have been assigned: *{readable_type}*\n"
            f"Week of: {week_of}\n\n"
            f"Reply ACCEPT or DECLINE"
        )
    else:
        return (
            f"📋 *{cong_name}*\n\n"
            f"Your assignment has been removed: *{readable_type}*\n"
            f"Week of: {week_of}"
        )


def _send_via_rapidpro(phone, flow_uuid, diff, api_token):
    """Trigger a RapidPro flow for notification delivery."""
    import requests

    rapidpro_url = getattr(settings, 'RAPIDPRO_URL', 'http://127.0.0.1:8080')
    urn = f"whatsapp:{phone.lstrip('+')}"

    response = requests.post(
        f"{rapidpro_url}/api/v2/flow_starts.json",
        headers={
            'Authorization': f'Token {api_token}',
            'Content-Type': 'application/json',
        },
        json={
            'flow': flow_uuid,
            'urns': [urn],
            'extra': diff,
        },
        timeout=10,
    )
    response.raise_for_status()


def _send_via_wuzapi(phone, message):
    """Send a direct WhatsApp message via WuzAPI."""
    import requests

    wuzapi_url = getattr(settings, 'WUZAPI_URL', 'http://127.0.0.1:8084')
    wuzapi_token = getattr(settings, 'WUZAPI_TOKEN', '')

    response = requests.post(
        f"{wuzapi_url}/chat/send/text",
        headers={
            'Authorization': f'Bearer {wuzapi_token}',
            'Content-Type': 'application/json',
        },
        json={
            'Phone': phone.lstrip('+'),
            'Body': message,
        },
        timeout=10,
    )
    response.raise_for_status()
