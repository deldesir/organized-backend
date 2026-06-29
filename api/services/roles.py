"""Role matrix for Organized users.

Direct port of getUserRoles() from sws2apps-api
users_controller.ts (lines 401-412).

Maps a user's cong_role array to derived boolean flags
that gate which backup tables they can access.
"""

ROLE_MASTER_KEY = ['admin', 'coordinator', 'secretary', 'elder', 'service_overseer']


def get_user_roles(cong_role):
    """Compute derived role flags from a user's cong_role array.

    Args:
        cong_role: list of role strings, e.g. ['admin', 'elder']

    Returns:
        dict of boolean flags used for data scoping
    """
    if not cong_role:
        cong_role = []

    admin = 'admin' in cong_role
    coordinator = 'coordinator' in cong_role
    secretary = 'secretary' in cong_role
    elder = 'elder' in cong_role
    service_overseer = 'service_overseer' in cong_role
    language_group = admin or 'language_group_overseers' in cong_role
    public_talk = language_group or 'public_talk_schedule' in cong_role
    service_committee = admin or coordinator or secretary or service_overseer

    return {
        'adminRole': admin,
        'coordinatorRole': coordinator,
        'secretaryRole': secretary,
        'elderRole': elder or coordinator or service_overseer or service_committee,
        'serviceOverseerRole': service_overseer,
        'serviceCommittee': service_committee,
        'scheduleEditor': admin or 'midweek_schedule' in cong_role or 'weekend_schedule' in cong_role,
        'attendanceTracker': service_committee or 'attendance_tracking' in cong_role,
        'reportEditorRole': service_committee or 'field_service_group_overseer' in cong_role,
        'publicTalkEditor': public_talk,
        'personViewer': admin or coordinator or secretary or elder or service_overseer,
        'personMinimal': not (admin or coordinator or secretary or elder or service_overseer),
        'isPublisher': 'publisher' in cong_role or any(
            r in cong_role for r in ROLE_MASTER_KEY
        ),
        'masterKeyNeed': any(r in cong_role for r in ROLE_MASTER_KEY),
    }
